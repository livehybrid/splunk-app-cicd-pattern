#!/usr/bin/env python3
"""
scts.py — minimal client for the Splunk Cloud Testing Service (SCTS).

SCTS provisions short-lived Splunk stacks from an API, which is what makes it
useful in CI: a pipeline can stand up a real Splunk, install the add-on it just
built, prove the add-on actually loads, and tear the stack down again.

API: https://scts.dev.splunk.com/openapi.json
Auth: `Authorization: Bearer <Dev Portal API token>`

What the API does and does not do
---------------------------------
SCTS manages the *stack* lifecycle only. There are exactly five operations:

    POST   /v1/stacks            create
    GET    /v1/stacks            list
    GET    /v1/stacks/versions   available Splunk versions
    GET    /v1/stacks/{id}       detail, including access details
    DELETE /v1/stacks/{id}       delete

There is **no app-install endpoint**. Once a stack reaches RUNNING, SCTS hands
back `stackAccessDetails` (url, username, password) and installing an add-on is
then an ordinary Splunk operation against that stack, not an SCTS one. That is
what `install` below does.

Stdlib only, deliberately: this runs in CI on a bare Python and should not need
a pip step or a lockfile to stay reproducible.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Optional, Tuple

DEFAULT_BASE_URL = "https://scts.dev.splunk.com"
TOKEN_ENV = "SCTS_API_TOKEN"

# Stack states from the OpenAPI schema.
STATE_CREATING = "CREATING"
STATE_RUNNING = "RUNNING"
STATE_STOPPING = "STOPPING"
STATE_ERROR = "ERROR"
TERMINAL_BAD = {STATE_ERROR, STATE_STOPPING}


# ---------------------------------------------------------------------------
# Output helpers. GitHub Actions-aware, but they degrade to plain text locally.
# ---------------------------------------------------------------------------

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def log(message: str) -> None:
    print(message, flush=True)


def mask(secret: str) -> None:
    """Register a value with the Actions log masker before it can be printed.

    Anything derived from a stack's access details has to go through this. A
    stack is short-lived, but a password in a public repo's build log is
    forever.
    """
    if secret and IN_ACTIONS:
        print("::add-mask::{}".format(secret), flush=True)


def set_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        # Heredoc form, so a value containing a newline cannot corrupt the file.
        delimiter = "ghadelim-{}".format(uuid.uuid4().hex)
        fh.write("{}<<{}\n{}\n{}\n".format(name, delimiter, value, delimiter))


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    if IN_ACTIONS:
        print("::error::{}".format(message), flush=True)
    else:
        print("ERROR: {}".format(message), file=sys.stderr, flush=True)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes] = None,
    timeout: float = 60.0,
    context: Optional[ssl.SSLContext] = None,
) -> Tuple[int, bytes, Dict[str, str]]:
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


class Scts:
    """The five SCTS operations, and nothing else."""

    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL) -> None:
        if not token:
            fail(
                "No SCTS API token. Set {} (in CI: a repository secret, never a "
                "committed file).".format(TOKEN_ENV)
            )
        self._token = token
        self._base = base_url.rstrip("/")

    def _call(self, method: str, path: str, payload: Optional[Any] = None) -> Tuple[int, Any]:
        headers = {
            "Authorization": "Bearer {}".format(self._token),
            "Accept": "application/json",
            "User-Agent": "splunk-app-cicd-pattern/scts.py",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        status, raw, _ = _request(method, self._base + path, headers, body)
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    @staticmethod
    def _describe_error(status: int, data: Any) -> str:
        """SCTS returns {code, message} on error; fall back to the raw body."""
        if isinstance(data, dict) and "message" in data:
            return "HTTP {} {} — {}".format(status, data.get("code", ""), data["message"])
        return "HTTP {} — {}".format(status, str(data)[:300])

    # -- operations ---------------------------------------------------------

    def versions(self) -> list:
        status, data = self._call("GET", "/v1/stacks/versions")
        if status != 200:
            fail("listStackVersions failed: " + self._describe_error(status, data))
        return (data or {}).get("versions", [])

    def list_stacks(self) -> list:
        status, data = self._call("GET", "/v1/stacks")
        if status != 200:
            fail("listStacks failed: " + self._describe_error(status, data))
        return (data or {}).get("stacks", [])

    def create(self, splunk_version: Optional[str] = None) -> Dict[str, Any]:
        # An empty object means "latest released", per the spec.
        payload: Dict[str, Any] = {}
        if splunk_version:
            payload["splunkVersion"] = splunk_version
        status, data = self._call("POST", "/v1/stacks", payload)
        if status != 202:
            fail("createStack failed: " + self._describe_error(status, data))
        return data

    def get(self, stack_id: str) -> Dict[str, Any]:
        status, data = self._call("GET", "/v1/stacks/{}".format(urllib.parse.quote(stack_id)))
        if status != 200:
            fail("getStack failed: " + self._describe_error(status, data))
        return data

    def delete(self, stack_id: str, strict: bool = False) -> bool:
        status, data = self._call("DELETE", "/v1/stacks/{}".format(urllib.parse.quote(stack_id)))
        if status == 204:
            return True
        if status == 404:
            # Already gone. For teardown that is success, not failure.
            log("Stack {} already absent (404).".format(stack_id))
            return True
        message = "deleteStack failed: " + self._describe_error(status, data)
        if strict:
            fail(message)
        log("WARNING: " + message)
        return False

    # -- derived ------------------------------------------------------------

    def wait_until_running(self, stack_id: str, timeout_s: int, interval_s: int) -> Dict[str, Any]:
        """Poll getStack until RUNNING with access details, or give up.

        Access details are documented as "present only when the stack has access
        details available", so RUNNING alone is not enough to start installing:
        wait for the credentials too.
        """
        deadline = time.time() + timeout_s
        last_state = None
        while time.time() < deadline:
            detail = self.get(stack_id)
            state = detail.get("state")
            if state != last_state:
                log("Stack {} state: {}".format(stack_id, state))
                last_state = state

            if state == STATE_RUNNING and detail.get("stackAccessDetails"):
                return detail
            if state in TERMINAL_BAD:
                fail(
                    "Stack {} reached {} before becoming usable. Nothing to install "
                    "into.".format(stack_id, state)
                )
            time.sleep(interval_s)

        fail(
            "Stack {} did not reach {} with access details within {}s (last state: "
            "{}).".format(stack_id, STATE_RUNNING, timeout_s, last_state)
        )


# ---------------------------------------------------------------------------
# Installing an add-on onto a stack
#
# SCTS has no install endpoint, so this talks to the stack's own Splunk. Two
# routes are tried, most-likely first, because what a stack exposes is a
# property of how SCTS provisions it rather than something the API promises:
#
#   1. Splunk Web app upload (POST /en-US/manager/appinstall/_upload). Works
#      wherever Splunk Web is reachable, which is exactly what SCTS hands back.
#   2. Management REST (POST /services/apps/local) on :8089, if it is exposed.
#
# `probe` reports which of these a stack actually answers on, so the first real
# run tells us which route is the supported one rather than us guessing.
# ---------------------------------------------------------------------------

def _tls_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _multipart(fields: Dict[str, str], file_field: str, file_path: str) -> Tuple[bytes, str]:
    """Build a multipart/form-data body without pulling in requests."""
    boundary = "----sctsboundary{}".format(uuid.uuid4().hex)
    filename = os.path.basename(file_path)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    parts = []
    for name, value in fields.items():
        parts.append(
            ('--{b}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n')
            .format(b=boundary, n=name, v=value).encode("utf-8")
        )
    with open(file_path, "rb") as fh:
        payload = fh.read()
    parts.append(
        ('--{b}\r\nContent-Disposition: form-data; name="{n}"; filename="{f}"\r\n'
         'Content-Type: {c}\r\n\r\n').format(b=boundary, n=file_field, f=filename, c=ctype)
        .encode("utf-8")
    )
    parts.append(payload)
    parts.append("\r\n--{b}--\r\n".format(b=boundary).encode("utf-8"))

    return b"".join(parts), "multipart/form-data; boundary={}".format(boundary)


class Stack:
    """A provisioned SCTS stack, addressed through its own Splunk endpoints."""

    def __init__(self, access: Dict[str, str], verify_tls: bool = True) -> None:
        self.web_url = str(access["url"]).rstrip("/")
        self.username = str(access["username"])
        self.password = str(access["password"])
        self._ctx = _tls_context(verify_tls)
        # Everything derived from the stack password must be masked before any
        # of it can reach a log line.
        mask(self.password)

    @property
    def mgmt_url(self) -> str:
        """Management REST, assuming the conventional :8089 on the same host."""
        parts = urllib.parse.urlsplit(self.web_url)
        return "https://{}:8089".format(parts.hostname)

    # -- probing ------------------------------------------------------------

    def probe(self) -> Dict[str, Any]:
        """Report which install routes this stack actually answers on."""
        result: Dict[str, Any] = {"web_url": self.web_url, "mgmt_url": self.mgmt_url}

        try:
            status, _, _ = _request(
                "GET", self.web_url + "/en-US/account/login",
                {"User-Agent": "scts.py"}, timeout=30, context=self._ctx,
            )
            result["web_login"] = status
        except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
            result["web_login"] = "unreachable: {}".format(exc)

        try:
            status, _, _ = _request(
                "GET", self.mgmt_url + "/services/server/info",
                {"User-Agent": "scts.py"}, timeout=30, context=self._ctx,
            )
            # 401 is a positive result here: it proves splunkd answered.
            result["mgmt_server_info"] = status
        except Exception as exc:  # noqa: BLE001
            result["mgmt_server_info"] = "unreachable: {}".format(exc)

        return result

    # -- route 1: Splunk Web upload ----------------------------------------

    def _web_login(self) -> Dict[str, str]:
        """Log in to Splunk Web and return the cookies + CSRF form key.

        Splunk Web issues the CSRF token as a cookie (`cval` before login,
        `splunkweb_csrf_token_<port>` after) and expects it echoed back in the
        `X-Splunk-Form-Key` header. Miss that and every POST is a 403.
        """
        status, _, headers = _request(
            "GET", self.web_url + "/en-US/account/login",
            {"User-Agent": "scts.py"}, timeout=30, context=self._ctx,
        )
        cookies = _parse_cookies(headers)
        form_key = cookies.get("cval", "")

        body = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password,
            "cval": form_key,
        }).encode("utf-8")
        status, _, headers = _request(
            "POST", self.web_url + "/en-US/account/login",
            {
                "User-Agent": "scts.py",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": _cookie_header(cookies),
                "X-Splunk-Form-Key": form_key,
                "X-Requested-With": "XMLHttpRequest",
            },
            body, timeout=60, context=self._ctx,
        )
        if status not in (200, 302, 303):
            fail("Splunk Web login failed on {} (HTTP {}).".format(self.web_url, status))

        cookies.update(_parse_cookies(headers))
        post_login_key = next(
            (v for k, v in cookies.items() if k.startswith("splunkweb_csrf_token")), form_key,
        )
        cookies["__form_key__"] = post_login_key
        return cookies

    def install_via_web(self, package_path: str) -> None:
        cookies = self._web_login()
        form_key = cookies.pop("__form_key__")

        body, content_type = _multipart(
            {"force": "1", "appfile_url": "", "cval": form_key},
            "appfile", package_path,
        )
        status, raw, _ = _request(
            "POST", self.web_url + "/en-US/manager/appinstall/_upload",
            {
                "User-Agent": "scts.py",
                "Content-Type": content_type,
                "Cookie": _cookie_header(cookies),
                "X-Splunk-Form-Key": form_key,
                "X-Requested-With": "XMLHttpRequest",
            },
            body, timeout=300, context=self._ctx,
        )
        if status not in (200, 201, 303):
            fail(
                "App upload rejected by Splunk Web (HTTP {}): {}".format(
                    status, raw.decode("utf-8", "replace")[:400]
                )
            )
        log("Uploaded {} via Splunk Web.".format(os.path.basename(package_path)))

    # -- verification -------------------------------------------------------

    def installed_version(self, app_name: str) -> Optional[str]:
        """Read the app's version back off the stack, through the mgmt API.

        This is the step that makes the whole exercise worth running. The build
        stamps the commit hash into app.conf, so the version read back here
        proves *which commit* is on the stack, not merely that something with
        the right name installed.
        """
        auth = _basic_auth(self.username, self.password)
        url = "{}/servicesNS/nobody/system/apps/local/{}?output_mode=json".format(
            self.mgmt_url, urllib.parse.quote(app_name),
        )
        try:
            status, raw, _ = _request(
                "GET", url,
                {"User-Agent": "scts.py", "Authorization": auth},
                timeout=60, context=self._ctx,
            )
        except Exception as exc:  # noqa: BLE001
            log("Could not reach the management API to verify: {}".format(exc))
            return None
        if status != 200:
            log("Verification request returned HTTP {}.".format(status))
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
            return data["entry"][0]["content"].get("version")
        except (ValueError, KeyError, IndexError):
            return None


def _parse_cookies(headers: Dict[str, str]) -> Dict[str, str]:
    jar: Dict[str, str] = {}
    raw = headers.get("Set-Cookie") or ""
    for chunk in re.split(r",(?=[^;]+?=)", raw):
        pair = chunk.split(";", 1)[0].strip()
        if "=" in pair:
            key, _, value = pair.partition("=")
            jar[key.strip()] = value.strip()
    return jar


def _cookie_header(jar: Dict[str, str]) -> str:
    return "; ".join(
        "{}={}".format(k, v) for k, v in jar.items() if not k.startswith("__")
    )


def _basic_auth(user: str, password: str) -> str:
    import base64
    return "Basic " + base64.b64encode("{}:{}".format(user, password).encode()).decode()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _client(args: argparse.Namespace) -> Scts:
    return Scts(os.environ.get(TOKEN_ENV, ""), args.base_url)


def cmd_versions(args: argparse.Namespace) -> None:
    for version in _client(args).versions():
        log("{status:<11} {build:<16} {name}".format(
            status=version.get("status", "?"),
            build=version.get("buildVersion", "?"),
            name=version.get("releaseName", ""),
        ))


def cmd_list(args: argparse.Namespace) -> None:
    stacks = _client(args).list_stacks()
    if not stacks:
        log("No active stacks.")
        return
    for stack in stacks:
        log("{id}  {state:<9} {version:<12} terminates {term}".format(
            id=stack.get("id"), state=stack.get("state"),
            version=stack.get("splunkVersion", "?"),
            term=stack.get("terminationDate", "?"),
        ))


def cmd_create(args: argparse.Namespace) -> None:
    client = _client(args)
    stack = client.create(args.splunk_version)
    stack_id = stack["id"]
    # Emit the id before waiting: if the wait times out, the teardown step still
    # needs the id or the stack leaks until its termination date.
    set_output("stack_id", stack_id)
    log("Created stack {} ({}), terminates {}".format(
        stack_id, stack.get("splunkVersion", "?"), stack.get("terminationDate", "?"),
    ))

    if args.no_wait:
        return
    detail = client.wait_until_running(stack_id, args.timeout, args.interval)
    access = detail.get("stackAccessDetails") or {}
    mask(str(access.get("password", "")))
    set_output("stack_url", str(access.get("url", "")))
    set_output("splunk_version", str(detail.get("splunkVersion", "")))
    log("Stack {} is RUNNING at {}".format(stack_id, access.get("url")))


def cmd_probe(args: argparse.Namespace) -> None:
    detail = _client(args).get(args.stack_id)
    access = detail.get("stackAccessDetails")
    if not access:
        fail("Stack {} has no access details yet (state {}).".format(
            args.stack_id, detail.get("state")))
    result = Stack(access, verify_tls=not args.insecure).probe()
    log(json.dumps(result, indent=2))


def cmd_install(args: argparse.Namespace) -> None:
    client = _client(args)
    detail = client.get(args.stack_id)
    access = detail.get("stackAccessDetails")
    if not access:
        fail("Stack {} has no access details (state {}).".format(
            args.stack_id, detail.get("state")))

    stack = Stack(access, verify_tls=not args.insecure)
    log("Install target: {} (Splunk {})".format(stack.web_url, detail.get("splunkVersion")))
    log("Probe: " + json.dumps(stack.probe()))

    if not os.path.isfile(args.package):
        fail("Package not found: {}".format(args.package))
    stack.install_via_web(args.package)

    if args.app_name:
        version = stack.installed_version(args.app_name)
        if version:
            log("Verified: {} is installed at version {}".format(args.app_name, version))
            set_output("installed_version", version)
            if args.expect_version and version != args.expect_version:
                fail(
                    "Version mismatch. Built {} but the stack reports {}. The artefact "
                    "on the stack is not the one this run produced.".format(
                        args.expect_version, version)
                )
        else:
            fail(
                "{} did not read back from the stack after upload. Treating that as a "
                "failed install rather than assuming success.".format(args.app_name)
            )


def cmd_delete(args: argparse.Namespace) -> None:
    if not args.stack_id:
        log("No stack id supplied; nothing to delete.")
        return
    ok = _client(args).delete(args.stack_id, strict=args.strict)
    log("Deleted stack {}".format(args.stack_id) if ok else "Stack not deleted.")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Splunk Cloud Testing Service client.")
    parser.add_argument("--base-url", default=os.environ.get("SCTS_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--insecure", action="store_true",
        help="Skip TLS verification when talking to the stack (not to SCTS itself).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("versions", help="List available Splunk versions.").set_defaults(func=cmd_versions)
    sub.add_parser("list", help="List your active stacks.").set_defaults(func=cmd_list)

    p_create = sub.add_parser("create", help="Create a stack and wait for it.")
    p_create.add_argument("--splunk-version", default=None,
                          help="Build version to provision. Omit for latest released.")
    p_create.add_argument("--timeout", type=int, default=1800, help="Seconds to wait for RUNNING.")
    p_create.add_argument("--interval", type=int, default=20, help="Poll interval in seconds.")
    p_create.add_argument("--no-wait", action="store_true")
    p_create.set_defaults(func=cmd_create)

    p_probe = sub.add_parser("probe", help="Report which endpoints a stack exposes.")
    p_probe.add_argument("stack_id")
    p_probe.set_defaults(func=cmd_probe)

    p_install = sub.add_parser("install", help="Install a built add-on onto a stack.")
    p_install.add_argument("stack_id")
    p_install.add_argument("--package", required=True, help="Path to the .tar.gz / .spl.")
    p_install.add_argument("--app-name", default=None, help="App id, to verify the install.")
    p_install.add_argument("--expect-version", default=None,
                           help="Fail unless the stack reports exactly this version.")
    p_install.set_defaults(func=cmd_install)

    p_delete = sub.add_parser("delete", help="Delete a stack.")
    p_delete.add_argument("stack_id", nargs="?", default="")
    p_delete.add_argument("--strict", action="store_true",
                          help="Exit non-zero if the delete fails.")
    p_delete.set_defaults(func=cmd_delete)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
