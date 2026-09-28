#!/usr/bin/env python3
"""
Fetch all downloadable documents + folder tree + descriptions from Blackboard
(Ultra or Original), authenticating via university SSO (Okta).

Auth technique borrowed from sanjacob/BlackboardSync: complete the SSO dance in
a real browser, harvest the Blackboard session cookies, then reuse them with
`requests` against the Learn REST API. Login success is detected when the
browser arrives at the configured `landing_page` while holding session cookies.

All options come from a YAML config file (default: ./config.yml).

Disk layout mirrors the Blackboard content tree; the course folder is named
after the course (as BlackboardSync does):
    out/
    ├── manifest.json / manifest.csv
    └── <Course Name>/
        ├── <Course Name>_desc.txt|.html      course description
        ├── links.html                        links at this folder level
        ├── <Folder>/                         'folder' AND 'lesson' types
        │   ├── <Folder>_desc.txt|.html       folder description (inside)
        │   ├── <Doc>_files/                  attachments of a document
        │   ├── <Doc>_desc.txt|.html          document description (outside)
        │   ├── file.pdf                      plain 'file' items
        │   └── <Custom>_info.json            unknown types: raw JSON dump
        └── ...
"""

import argparse
import base64
import csv
import hashlib
import hmac
import html
import json
import re
import struct
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
import yaml
from playwright.sync_api import sync_playwright

SESSION_COOKIES = {"BbRouter", "JSESSIONID"}
API = "/learn/api/public/v1"

# ---- content classification ----
FOLDER_TYPES = {"folder", "lesson"}            # lessons treated as folders
DOCUMENT_TYPE = "document"
FILE_TYPE = "file"
LINK_TYPES = {                                 # aggregated into links.html
    "externallink", "toollink", "blti-link",
    "assignment", "asmt-test-link", "asmt-survey-link", "achievement",
}
# everything else ('' blank pages, scormengine, unknown blti placements...)
# is "custom": dumped as JSON, recursed if it has children.


# ---------------------------------------------------------------- config ---

CONFIG_TEMPLATE = """\
# Blackboard instance
base_url: https://edimension.sutd.edu.sg
landing_page: https://edimension.sutd.edu.sg/ultra/stream

# Output
out_dir: bb_downloads
state_file: bb_state.json

# Behaviour
courses: ""            # substring filter, empty = all courses
download: true         # false = manifest only
login_timeout: 600     # seconds to wait for Okta/SSO/MFA
force_login: false     # true = skip saved session, always open browser

# SSO autofill (port of github.com/pcider/auto-login-otp); leave empty to type manually
login_host: ease.sutd.edu.sg   # only autofill on this host
username: ""
password: ""
totp_secret: ""        # <SECRET> from otpauth://...?secret=<SECRET> (or the whole URL)
auto_submit: true      # click the submit button for you
"""


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"[!] Config file not found: {p}\n")
        print("[!] Create one, e.g.:\n" + CONFIG_TEMPLATE)
        sys.exit(1)
    cfg = yaml.safe_load(p.read_text()) or {}

    for req in ("base_url",):
        if req not in cfg:
            print(f"[!] config.yml is missing required key '{req}'")
            sys.exit(1)

    cfg["base_url"] = str(cfg["base_url"]).rstrip("/")

    # landing_page: default to /ultra/stream; allow scheme-relative paths
    lp = str(cfg.get("landing_page") or cfg["base_url"] + "/ultra/stream")
    if lp.startswith("/"):
        lp = cfg["base_url"] + lp
    cfg["landing_page"] = lp.rstrip("/")

    cfg.setdefault("out_dir", "bb_downloads")
    cfg.setdefault("state_file", "bb_state.json")
    cfg.setdefault("courses", "")
    cfg.setdefault("download", True)
    cfg.setdefault("login_timeout", 600)
    cfg.setdefault("force_login", False)
    cfg.setdefault("login_host", "ease.sutd.edu.sg")
    for k in ("username", "password", "totp_secret"):
        cfg[k] = str(cfg.get(k) or "")
    cfg.setdefault("auto_submit", True)
    return cfg


# ---------------------------------------------------------------- auth -----

class SessionExpired(Exception):
    pass


# Okta sign-in widget selectors (from pcider/auto-login-otp)
USERNAME_FIELD_SELECTOR = "input[autocomplete='username']"
PASSWORD_FIELD_SELECTOR = "input[type='password']"
SUBMIT_BUTTON_SELECTOR = "input[type='submit']"
TOTP_FIELD_SELECTOR = "input[name='credentials.passcode']"
ERROR_ICON_SELECTOR = ".error-16"


def totp(secret: str, period: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (SHA-1). Accepts a base32 secret or a full otpauth:// URL."""
    if secret.startswith("otpauth://"):
        secret = parse_qs(urlparse(secret).query)["secret"][0]
    secret = secret.replace(" ", "").upper()
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(time.time()) // period),
                   hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % 10 ** digits).zfill(digits)


class SSOAutofill:
    """Fills the Okta username/password/TOTP pages, one poll at a time."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = any(cfg[k] for k in ("username", "password", "totp_secret"))
        self.last = (None, 0.0)   # (step, time) of last submit, avoids double-clicks
        self.last_code = None     # Okta rejects a reused TOTP code

    def _submit(self, page, step, btn):
        if not self.cfg["auto_submit"]:
            return
        prev_step, prev_t = self.last
        if step == prev_step and time.time() - prev_t < 5:
            return
        print(f"[*] Autofill: submitting {step}")
        btn.click()
        self.last = (step, time.time())

    def poll(self, page):
        if not self.enabled or self.cfg["login_host"] not in urlparse(page.url).netloc:
            return
        sel = page.query_selector
        login_input = sel(USERNAME_FIELD_SELECTOR)
        pw_input = sel(PASSWORD_FIELD_SELECTOR)
        submit_btn = sel(SUBMIT_BUTTON_SELECTOR)
        totp_input = sel(TOTP_FIELD_SELECTOR)

        if sel(ERROR_ICON_SELECTOR):
            print("[!] Autofill: login error detected, bailing out; finish manually.")
            self.enabled = False
            return
        if not submit_btn:
            return

        if login_input and pw_input:
            if self.cfg["username"] and not login_input.input_value():
                login_input.fill(self.cfg["username"])
            if self.cfg["password"] and not pw_input.input_value():
                pw_input.fill(self.cfg["password"])
            if login_input.input_value() and pw_input.input_value():
                self._submit(page, "username+password", submit_btn)
        elif pw_input:
            if self.cfg["password"] and not pw_input.input_value():
                pw_input.fill(self.cfg["password"])
            if pw_input.input_value():
                self._submit(page, "password", submit_btn)
        elif totp_input:
            if self.cfg["totp_secret"] and not totp_input.input_value():
                code = totp(self.cfg["totp_secret"])
                if code == self.last_code:
                    return  # wait for the next 30s window
                totp_input.fill(code)
                self.last_code = code
            if totp_input.input_value():
                self._submit(page, "TOTP", submit_btn)


def browser_login(cfg: dict, state_path: Path):
    """Open a real browser at the landing page, let the user complete
    Okta/SSO/MFA, and harvest the session cookies once we land back on the
    landing page."""
    base_url = cfg["base_url"]
    landing = cfg["landing_page"]

    print(f"\n[*] Opening browser at {landing}")
    print("[*] Complete your university login (Okta/MFA). Waiting...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        # Unauthenticated requests to the landing page redirect to the SSO
        # login flow; after auth, Blackboard returns us to the landing page.
        page.goto(landing, wait_until="domcontentloaded")

        autofill = SSOAutofill(cfg)
        bb_host = urlparse(base_url).netloc
        chooser_done = None   # URL where process('netId') was last called
        deadline = time.time() + cfg["login_timeout"]
        while time.time() < deadline:
            try:
                # Blackboard's login chooser: pick "NetID" to go to EASE
                if (urlparse(page.url).netloc == bb_host and page.url != chooser_done
                        and page.evaluate("typeof process === 'function'")):
                    print("[*] Login chooser: calling process('netId')")
                    chooser_done = page.url
                    page.evaluate("process('netId')")
                autofill.poll(page)
                on_landing = page.url.startswith(landing)
                cookie_names = {c["name"] for c in context.cookies(base_url)}
                if on_landing and (SESSION_COOKIES & cookie_names):
                    context.storage_state(path=str(state_path))
                    print(f"[*] Landed on {page.url}")
                    print(f"[*] Login captured, session saved to {state_path}")
                    browser.close()
                    return
            except Exception:
                pass  # page mid-navigation
            page.wait_for_timeout(500)

        browser.close()
        raise TimeoutError(
            f"Did not reach the configured landing page ({landing}) "
            f"within {cfg['login_timeout']}s.")


def session_from_state(state_path: Path) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "application/json, */*",
    })
    state = json.loads(state_path.read_text())
    for c in state["cookies"]:
        s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return s


def check_session(s: requests.Session, base_url: str) -> bool:
    try:
        r = s.get(base_url + API + "/users/me", timeout=20)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ----------------------------------------------------------------- api -----

def api_get(s: requests.Session, url: str, params=None, retries: int = 4):
    for attempt in range(retries):
        r = s.get(url, params=params, timeout=30)
        if r.status_code == 401:
            raise SessionExpired
        if r.status_code in (429, 503):
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()


def paged(s: requests.Session, url: str, params=None):
    """Yield all items from a paginated Learn REST collection."""
    while url:
        data = api_get(s, url, params=params).json()
        yield from data.get("results", [])
        url = data.get("paging", {}).get("nextPage")
        params = None  # nextPage already encodes params
        time.sleep(0.15)


def get_me(s: requests.Session, base_url: str) -> dict:
    try:
        return api_get(s, base_url + API + "/users/me").json()
    except requests.HTTPError:
        return api_get(s, base_url + "/learn/api/v1/users/me").json()


def get_courses(s: requests.Session, base_url: str, user_id: str):
    url = f"{base_url}{API}/users/{user_id}/courses"
    for m in paged(s, url, params={"limit": 100, "expand": "course"}):
        cid = m.get("courseId")
        course = m.get("course") or {}
        if not course.get("name"):
            try:
                course = api_get(s, f"{base_url}{API}/courses/{cid}").json()
            except requests.HTTPError:
                course = {"id": cid, "name": cid}
        course.setdefault("id", cid)
        yield course


def list_contents(s, base_url, course_id, parent_id=None):
    if parent_id is None:
        url = f"{base_url}{API}/courses/{course_id}/contents"
    else:
        url = f"{base_url}{API}/courses/{course_id}/contents/{parent_id}/children"
    return paged(s, url, params={"limit": 200})


def list_attachments(s, base_url, course_id, content_id) -> list[dict]:
    url = f"{base_url}{API}/courses/{course_id}/contents/{content_id}/attachments"
    try:
        data = api_get(s, url).json()
    except requests.HTTPError:
        return []
    if "results" in data:
        return data["results"]
    if "id" in data:
        return [data]
    return []


# ------------------------------------------------------------ helpers ------

def sanitize(name: str) -> str:
    name = html.unescape(name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = name.strip(" .")          # no trailing dots (Windows-safe)
    return name[:120] or "untitled"


def strip_html(raw: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", html.unescape(raw or ""))
    return re.sub(r"\s+", " ", txt).strip()


def bbml_file_links(body: str):
    """Ultra documents embed attachments as BBML anchors -> [(url, filename)]."""
    out = []
    if not body:
        return out
    for m in re.finditer(r'<a\s+[^>]*href="([^"]*bbcswebdav[^"]*)"[^>]*>(.*?)</a>',
                         body, re.S):
        url = html.unescape(m.group(1))
        segment = m.group(0)
        name = None
        nm = re.search(r'"(?:linkName|alternativeText)":"([^"]+)"', segment)
        if nm:
            name = nm.group(1)
        if not name and "." in strip_html(m.group(2)):
            name = strip_html(m.group(2))
        out.append((url, unquote(name or f"attachment_{len(out) + 1}")))
    return out


class NameClash:
    """Hands out unique file/dir names per directory (in-run collisions)."""

    def __init__(self):
        self._used = set()

    def claim(self, directory: Path, name: str) -> str:
        key = (str(directory).lower(), name.lower())
        if key not in self._used:
            self._used.add(key)
            return name
        if "." in name[1:]:
            stem, suffix = name.rsplit(".", 1)
            suffix = "." + suffix
        else:
            stem, suffix = name, ""
        i = 2
        while (str(directory).lower(), f"{stem} ({i}){suffix}".lower()) in self._used:
            i += 1
        alt = f"{stem} ({i}){suffix}"
        self._used.add((str(directory).lower(), alt.lower()))
        return alt


class Ctx:
    def __init__(self, s, base_url, course_id, course_name, claimer,
                 rows, errors, download):
        self.s, self.base_url, self.course_id = s, base_url, course_id
        self.course_name, self.claimer = course_name, claimer
        self.rows, self.errors, self.download = rows, errors, download

    def launch_url(self, content_id):
        return (f"{self.base_url}/webapps/blackboard/content/launchLink.jsp"
                f"?course_id={self.course_id}&content_id={content_id}")


def prune_empty_dirs(root: Path) -> int:
    """Remove empty directories under root (bottom-up), keeping root itself."""
    removed = 0
    for d in sorted((p for p in root.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
            removed += 1
    return removed


def save_stream(s, url, dest: Path, errors):
    if dest.exists() and dest.stat().st_size > 0:
        return "skipped (exists)"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with s.get(url, stream=True, allow_redirects=True, timeout=120) as r:
            if r.status_code == 401:
                raise SessionExpired
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
            tmp.replace(dest)
        time.sleep(0.3)
        return "ok"
    except SessionExpired:
        raise
    except Exception as e:  # noqa: BLE001
        errors.append(f"{url} -> {e}")
        return f"error: {e}"


# ------------------------------------------------------ disk emitters -----

def make_dir(ctx, parent_dir, title, suffix=""):
    d = parent_dir / ctx.claimer.claim(parent_dir, sanitize(title) + suffix)
    if ctx.download:
        d.mkdir(parents=True, exist_ok=True)
    return d


def write_desc(ctx, directory, title, body, entry):
    if not ctx.download or not (body or "").strip():
        return
    safe = sanitize(title)
    p1 = directory / ctx.claimer.claim(directory, f"{safe}_desc.txt")
    p2 = directory / ctx.claimer.claim(directory, f"{safe}_desc.html")
    p1.write_text(strip_html(body) + "\n", encoding="utf-8")
    p2.write_text(f'<!doctype html><meta charset="utf-8">'
                  f'<title>{html.escape(title)}</title>\n{body}\n',
                  encoding="utf-8")
    entry["artifacts"].extend([p1.name, p2.name])


def dump_info(ctx, directory, title, node, entry):
    if not ctx.download:
        return
    p = directory / ctx.claimer.claim(directory, f"{sanitize(title)}_info.json")
    p.write_text(json.dumps(node, indent=2, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    entry["artifacts"].append(p.name)


def write_links_html(ctx, directory, heading, links):
    if not ctx.download or not links:
        return
    items = []
    for l in links:
        url = html.escape(l.get("url") or "", quote=True)
        desc = html.escape(l.get("description_text") or "")
        block = (f'  <li><a href="{url}" target="_blank">'
                 f'{html.escape(l["title"])}</a> '
                 f'<small>[{html.escape(l["type"] or "?")}]</small>')
        if desc:
            block += f'\n    <p class="desc">{desc}</p>'
        items.append(block + "</li>")
    page = ('<!doctype html>\n<html><head><meta charset="utf-8">'
            f'<title>Links — {html.escape(heading)}</title>\n'
            '<style>body{font-family:system-ui,sans-serif;max-width:48em;'
            'margin:2em auto;padding:0 1em}ul{padding-left:1.2em}'
            'small{color:#777}.desc{color:#444;margin:.15em 0 .8em}</style>\n'
            f'</head>\n<body>\n<h1>Links in {html.escape(heading)}</h1>\n<ul>\n'
            + "\n".join(items) + "\n</ul>\n</body></html>\n")
    p = directory / ctx.claimer.claim(directory, "links.html")
    p.write_text(page, encoding="utf-8")


def write_course_desc(ctx, course_dir, cname, course, cnode):
    body = (course.get("description") or "").strip()
    if not ctx.download or not body:
        return
    p1 = course_dir / ctx.claimer.claim(course_dir, f"{sanitize(cname)}_desc.txt")
    p2 = course_dir / ctx.claimer.claim(course_dir, f"{sanitize(cname)}_desc.html")
    p1.write_text(body + "\n", encoding="utf-8")
    p2.write_text(f'<!doctype html><meta charset="utf-8">'
                  f'<p>{html.escape(body)}</p>\n', encoding="utf-8")
    cnode["description"] = body


# --------------------------------------------------------------- crawler ---

def classify(t: str) -> str:
    t = (t or "").strip()
    if t in FOLDER_TYPES:
        return "folder"
    if t == DOCUMENT_TYPE:
        return "document"
    if t == FILE_TYPE:
        return "file"
    if t in LINK_TYPES or t.startswith("bltiplacement-") or t.endswith("basiclti"):
        return "link"
    return "custom"


def gather_attachments(ctx, node):
    cid = node.get("id")
    atts = []
    for att in list_attachments(ctx.s, ctx.base_url, ctx.course_id, cid):
        atts.append({
            "url": (f"{ctx.base_url}{API}/courses/{ctx.course_id}/contents/{cid}"
                   f"/attachments/{att['id']}/download"),
            "name": att.get("fileName") or f"attachment_{att.get('id', 'x')}",
            "endpoint": True,
        })
    for url, name in bbml_file_links(node.get("body") or ""):
        if url.startswith("/"):
            url = ctx.base_url + url
        atts.append({"url": url, "name": name, "endpoint": False})

    out, seen_urls, seen_names = [], set(), set()
    for a in atts:  # Ultra lists the same file in both the endpoint and BBML
        n = sanitize(a["name"]).lower()
        if a["url"] in seen_urls or (not a["endpoint"] and n in seen_names):
            continue
        seen_urls.add(a["url"])
        seen_names.add(n)
        out.append(a)
    return out


def download_attachment(ctx, directory, att, entry):
    name = ctx.claimer.claim(directory, sanitize(att["name"]))
    dest = directory / name
    if ctx.download:
        status = save_stream(ctx.s, att["url"], dest, ctx.errors)
        time.sleep(0.1)
    else:
        status = "listed"
    entry["files"].append({"name": name, "url": att["url"],
                           "saved_to": str(dest), "status": status})


def add_row(ctx, entry, breadcrumb):
    ctx.rows.append({
        "course": ctx.course_name,
        "path": " / ".join(breadcrumb),
        "title": entry["title"],
        "type": entry["type"] or "",
        "files": "; ".join(f["name"] for f in entry["files"]),
        "url": entry["url"] or "",
        "description": entry["description_text"],   # last column
    })


def process_children(ctx, parent_entry, parent_dir, breadcrumb, children_iter):
    links = []
    for child in children_iter:
        entry = process_node(ctx, child, parent_dir, breadcrumb)
        parent_entry["children"].append(entry)
        add_row(ctx, entry, breadcrumb)
        if entry["class"] == "link":
            links.append(entry)
    write_links_html(ctx, parent_dir, " / ".join(breadcrumb) or ctx.course_name,
                     links)


def process_node(ctx, node, parent_dir, breadcrumb):
    handler = node.get("contentHandler") or {}
    t = (handler.get("id") or "").replace("resource/x-bb-", "")
    cls = classify(t)
    title = node.get("title") or "(untitled)"
    body = node.get("body") or ""
    cid = node.get("id")

    entry = {
        "id": cid, "title": title, "type": t, "class": cls,
        "description_html": body, "description_text": strip_html(body),
        "url": None, "files": [], "artifacts": [], "children": [],
    }

    def recurse_into(dir_path):
        if node.get("hasChildren"):
            process_children(ctx, entry, dir_path, breadcrumb + [title],
                             list_contents(ctx.s, ctx.base_url, ctx.course_id, cid))

    # ---- links (incl. assignments, tests, LTI) -> one links.html per folder
    if cls == "link":
        entry["url"] = handler.get("url") or ctx.launch_url(cid)
        return entry

    # ---- folders & lessons -> real directories
    if cls == "folder":
        d = make_dir(ctx, parent_dir, title) if ctx.download else parent_dir
        write_desc(ctx, d, title, body, entry)      # desc lives inside the folder
        recurse_into(d)
        return entry

    # ---- documents -> {title}_files/ + desc files next to it
    if cls == "document":
        atts = gather_attachments(ctx, node)
        if ctx.download:
            write_desc(ctx, parent_dir, title, body, entry)
            if atts:
                fd = make_dir(ctx, parent_dir, title, suffix="_files")
                for a in atts:
                    download_attachment(ctx, fd, a, entry)
        else:
            for a in atts:
                download_attachment(ctx, parent_dir, a, entry)
        recurse_into(make_dir(ctx, parent_dir, title) if ctx.download else parent_dir)
        return entry

    # ---- plain file items -> saved directly into the current folder
    if cls == "file":
        atts = gather_attachments(ctx, node)
        if ctx.download:
            write_desc(ctx, parent_dir, title, body, entry)
            if len(atts) == 1:
                download_attachment(ctx, parent_dir, atts[0], entry)
            elif atts:
                fd = make_dir(ctx, parent_dir, title, suffix="_files")
                for a in atts:
                    download_attachment(ctx, fd, a, entry)
        else:
            for a in atts:
                download_attachment(ctx, parent_dir, a, entry)
        recurse_into(make_dir(ctx, parent_dir, title) if ctx.download else parent_dir)
        return entry

    # ---- custom types: dump everything we know into one file
    atts = gather_attachments(ctx, node)
    if ctx.download:
        if node.get("hasChildren"):
            d = make_dir(ctx, parent_dir, title)
            dump_info(ctx, d, title, node, entry)
            write_desc(ctx, d, title, body, entry)
            if atts:
                fd = make_dir(ctx, d, title, suffix="_files")
                for a in atts:
                    download_attachment(ctx, fd, a, entry)
            recurse_into(d)
        else:
            dump_info(ctx, parent_dir, title, node, entry)
            write_desc(ctx, parent_dir, title, body, entry)
            if atts:
                fd = make_dir(ctx, parent_dir, title, suffix="_files")
                for a in atts:
                    download_attachment(ctx, fd, a, entry)
    else:
        for a in atts:
            download_attachment(ctx, parent_dir, a, entry)
        recurse_into(parent_dir)
    return entry


# ----------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.yml",
                    help="path to the YAML config file (default: ./config.yml)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    base_url = cfg["base_url"]
    state_path = Path(cfg["state_file"])
    out_dir = Path(cfg["out_dir"])
    out_dir.mkdir(exist_ok=True)
    download = bool(cfg["download"])

    # --- auth: reuse saved cookies if still valid, else browser login ---
    s = None
    if state_path.exists() and not cfg["force_login"]:
        s = session_from_state(state_path)
        if not check_session(s, base_url):
            print("[*] Saved session expired, logging in again...")
            s = None
    if s is None:
        browser_login(cfg, state_path)
        s = session_from_state(state_path)

    me = get_me(s, base_url)
    user_id = me.get("id")
    print(f"[*] Logged in as {me.get('userName', user_id)}")

    course_claimer = NameClash()
    manifest, rows, errors = [], [], []

    for course in get_courses(s, base_url, user_id):
        cname = course.get("name") or course.get("courseId") or course["id"]
        if cfg["courses"] and cfg["courses"].lower() not in cname.lower():
            continue
        print(f"[*] Course: {cname}")
        # BlackboardSync-style: one folder per course, named after the course
        course_dir = out_dir / course_claimer.claim(out_dir, sanitize(cname))
        if download:
            course_dir.mkdir(parents=True, exist_ok=True)

        for attempt in (1, 2):   # one re-auth retry if session dies mid-course
            course_rows = []
            ctx = Ctx(s, base_url, course["id"], cname, NameClash(),
                      course_rows, errors, download)
            cnode = {"course_id": course["id"], "course": cname, "children": []}
            write_course_desc(ctx, course_dir, cname, course, cnode)
            try:
                process_children(ctx, cnode, course_dir, [cname],
                                list_contents(s, base_url, course["id"]))
                manifest.append(cnode)
                rows.extend(course_rows)
                print(f"    {len(course_rows)} items")
                break
            except SessionExpired:
                if attempt == 1:
                    print("[!] Session expired mid-run; re-authenticating...")
                    browser_login(cfg, state_path)
                    s = session_from_state(state_path)
                else:
                    errors.append(f"course {cname}: session expired twice, skipped")

    if download:
        pruned = prune_empty_dirs(out_dir)
        if pruned:
            print(f"[*] Removed {pruned} empty folders")

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False))
    with open(out_dir / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["course", "path", "title", "type",
                                          "files", "url", "description"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n[✓] Done: {len(rows)} items catalogued, {len(errors)} errors.")
    print(f"[✓] Tree + descriptions: {out_dir / 'manifest.json'} / manifest.csv")
    print(f"[✓] Files under: {out_dir}/")
    if errors:
        print("[!] Errors:", *errors[:20], sep="\n    - ")


if __name__ == "__main__":
    sys.exit(main())
