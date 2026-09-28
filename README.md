# bb_sync

Download everything from your SUTD eDimension (Blackboard) courses: files, the folder structure, and the text descriptions attached to folders and documents.

Login goes through the real SSO page in a browser window. If you put your credentials and TOTP secret in the config, the script fills them in and submits for you, so a sync can run without you touching the keyboard. After login, the session is saved and reused until it expires.

## Requirements

- Python 3.10 or newer
- `playwright`, `requests`, `pyyaml`
- A Chromium build for Playwright

```sh
python3 -m venv venv
source venv/bin/activate
pip install playwright requests pyyaml
playwright install chromium
```

## Setup

Copy the example config and edit it:

```sh
cp config-example.yml config.yml
```

`config.yml` is listed in `.gitignore` because it can contain your password and TOTP secret. Keep it out of anything you share.

## Usage

```sh
python bb_sync.py
```

Use a different config file with `--config`:

```sh
python bb_sync.py --config other.yml
```

What happens on a run:

1. If `state.json` holds a session that still works, it is reused and no browser opens.
2. Otherwise a Chromium window opens at the landing page. The script picks the EASE option on the Blackboard login page, which takes you to EASE.
3. On EASE, the username, password and one-time code are filled in from the config. Anything left empty in the config, you type yourself.
4. Once the browser lands back on the landing page with a valid session, the cookies are saved to `state.json` and the window closes.
5. Every course you are enrolled in is crawled and downloaded.

If the session expires partway through, the script logs in again once and carries on.

## Configuration

| Key | Default | Description |
| --- | --- | --- |
| `base_url` | (required) | Blackboard instance, e.g. `https://edimension.sutd.edu.sg` |
| `landing_page` | `<base_url>/ultra/stream` | Page the browser ends up on after login. Reaching it counts as a successful login. |
| `out_dir` | `bb_downloads` | Where downloads and manifests go |
| `state_file` | `bb_state.json` | Where the saved session is stored |
| `courses` | `""` | Only sync courses whose name contains this text (case-insensitive). Empty means all courses. |
| `download` | `true` | `false` writes the manifests only, without downloading files |
| `login_timeout` | `600` | Seconds to wait for login to finish |
| `force_login` | `false` | Ignore the saved session and always log in again |
| `desc_html_only` | `false` | Save descriptions as `_desc.html` only, without the plain-text `_desc.txt` copy |
| `login_host` | `ease.sutd.edu.sg` | Autofill only runs on this host, so your password is never typed into another site |
| `username` | `""` | EASE username (student ID) |
| `password` | `""` | EASE password |
| `totp_secret` | `""` | Secret for the one-time code (see below) |
| `auto_submit` | `true` | Click the submit button after filling in each step |
| `log_level` | `INFO` | Lowest level shown on the console (stderr): `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `log_dir` | `logs` | Folder for the per-run log files |
| `log_retention` | `7d` | Log files older than this are deleted at the start of each run. A number followed by `h`, `d` or `w` (e.g. `12h`, `7d`, `2w`). `0` keeps all logs. |

### Getting the TOTP secret

When you set up the authenticator for EASE, the QR code contains a URL like:

```
otpauth://totp/ease.sutd.edu.sg:<STUDENT_ID>?secret=<SECRET>&issuer=ease.sutd.edu.sg
```

Put either the `<SECRET>` part or the whole URL in `totp_secret`. If you already set up your authenticator app, some apps let you export or view this URL; otherwise you can re-enrol the authenticator on EASE to see the QR code again.

Anyone with this secret can generate your login codes. Treat it like a password.

## Output

```
downloads/
├── manifest.json
├── manifest.csv
└── <Course Name>/
    ├── <Course Name>_desc.txt / .html     course description
    ├── links.html                         links, assignments, tests and LTI tools at this level
    ├── <Folder>/                          folders and lessons
    │   ├── <Folder>_desc.txt / .html      folder description
    │   ├── <Doc>_desc.txt / .html         document description
    │   ├── <Doc>_files/                   attachments of a document
    │   ├── file.pdf                       plain file items
    │   └── <Item>_info.json               raw data for item types the script does not recognise
    └── ...
```

- `manifest.json` holds the full content tree with descriptions and where each file was saved.
- `manifest.csv` has one row per item: course, path, title, type, files, URL and description.
- Characters that are not allowed in filenames (such as `:` and `/`) are replaced with `_`.
- Downloaded files get the server's last-modified time as their own. On later runs the script reads only the response headers, and if the server's last-modified time (and size, when the server sends one) match the local copy, the download is skipped.
- Existing files are never overwritten. When a file does need downloading, it is compared with the copy on disk:
  - identical: left as it is
  - different: the old copy is renamed to `<name>_<DDMMYYYY_HHMMSS>.<ext>`, using its last-modified time, and the new version takes the original name (if that name is already taken, `_2`, `_3` and so on is added)

  Descriptions, `links.html` and `_info.json` files are handled the same way. Blackboard re-signs every file link each time the API is called, so these files are compared with the signing parameters removed; a file only counts as changed when its actual content changed. `manifest.json` and `manifest.csv` are simply replaced each run.
- Empty folders are removed at the end of a run, and each one removed is logged.

## Logging

Status messages, warnings and errors go to stderr, filtered by `log_level`. Set it to `WARNING` for a quiet run, or `DEBUG` to see every API request and every skip decision.

Every run also writes a full log at `DEBUG` level, whatever `log_level` is set to, to `<log_dir>/DDMMYY_HHMMSS.log`. When the run ends, the log is compressed to `DDMMYY_HHMMSS.log.gz` (read it with `zless` or `gzip -dc`). A plain `.log` left behind by a run that crashed is compressed at the start of the next run. Unexpected crashes are recorded with a full traceback. At the start of each run, logs older than `log_retention` are deleted.

Files that return 404 are logged as warnings and listed at the end. This usually means the instructor has made the item visible but not released the file itself, or the link points into an old course that no longer exists. Nothing on your side can fix these.

## How it works

Authentication follows the approach used by [BlackboardSync](https://github.com/sanjacob/BlackboardSync): log in with a real browser, take the Blackboard session cookies, and use them with `requests` against the Blackboard Learn REST API.

The SSO autofill is a port of the [Auto Login & OTP](https://github.com/pcider/auto-login-otp) userscript (included in `auto-login-otp/`). The one-time codes are generated with the Python standard library, so no extra OTP package is needed.

## Troubleshooting

- **The browser stays on the login page.** Check `username`, `password` and `totp_secret`. If EASE shows an error, autofill stops and leaves the rest to you so it does not keep submitting wrong details.
- **Login times out.** The login only counts as complete when the browser reaches `landing_page`. Make sure it matches where eDimension actually sends you after login, or raise `login_timeout`.
- **The code is rejected.** TOTP depends on your computer's clock. Make sure it is set automatically.
- **Something looks stale or broken after login.** Set `force_login: true` for one run, or delete `state.json`.
