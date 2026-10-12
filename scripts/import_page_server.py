#!/usr/bin/env python3
"""Serve a token-protected manual snapshot drop page over Tailscale."""
from __future__ import annotations

import argparse
import email.policy
import email.utils
import html
import json
import re
import secrets
import shlex
import sys
import tempfile
from datetime import datetime, timezone
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from jobs.common import page_fetch as pf


def without_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def needs_manual_save(entry: dict) -> bool:
    reason = entry.get("reason", "").lower()
    status = entry.get("status")
    mime = (entry.get("content_type") or "").split(";", 1)[0].strip().lower()
    if (entry.get("category") == "url_rejection" or status in {400, 404, 410}
            or "empty visible text" in reason or "unsupported content type" in reason
            or (mime and mime not in {"text/html", "application/xhtml+xml", "text/plain", "application/pdf"})):
        return False
    return "bot challenge" in reason or (status is not None and not 200 <= status < 300)


def upload_limit(name: str) -> int:
    return 50 * 1024 * 1024 if Path(name).suffix.lower() in {".mhtml", ".mht"} else pf.MAX_BYTES


def saved_document(name: str, body: bytes) -> tuple[str | None, bytes, str, str | None]:
    """Extract main MHTML HTML or the original URL of a Chrome HTML save."""
    limit = upload_limit(name)
    if len(body) > limit:
        raise ValueError(f"saved page exceeds {limit // (1024 * 1024)} MiB")
    suffix = Path(name).suffix.lower()
    if suffix in {".mhtml", ".mht"}:
        message = BytesParser(policy=email.policy.default).parsebytes(body)
        parts = [part for part in message.walk() if part.get_content_type() == "text/html"
                 and not part.is_multipart()]
        original = message.get("Snapshot-Content-Location") or message.get("Content-Location")
        if original:
            original = str(original).strip()
            matching = [part for part in parts if part.get("Content-Location")
                        and without_fragment(str(part["Content-Location"]).strip()) == without_fragment(original)]
            parts = matching or [part for part in parts if not part.get("Content-Location")]
        if not parts:
            raise ValueError("MHTML に主文書の text/html がありません")
        part = parts[0]
        original = original or part.get("Content-Location")
        original = str(original).strip() if original else None
        payload = part.get_payload(decode=True)
        if payload is None:
            raise ValueError("MHTML の HTML を読み取れません")
        encoding = part.get_content_charset() or "utf-8"
        try:
            payload = payload.decode(encoding, errors="replace").encode("utf-8")
        except LookupError as exc:
            raise ValueError("MHTML の文字コードが不明です") from exc
        stamp = None
        if message.get("Date"):
            try:
                date = email.utils.parsedate_to_datetime(str(message["Date"]))
                if date.utcoffset() is None:
                    raise ValueError("timezone missing")
                stamp = date.astimezone(timezone.utc).isoformat(timespec="seconds")
            except (ValueError, TypeError, OverflowError) as exc:
                raise ValueError("MHTML Date が不正です") from exc
        return original, payload, "saved.html", stamp
    match = re.search(br"<!--\s*saved from url=\(\d+\)(.*?)\s*-->", body, re.I | re.S)
    original = match.group(1).decode("utf-8", errors="replace").strip() if match else None
    return original, body, "saved" + suffix, None


def match_url(original: str | None, target: str | None, rows: list[dict]) -> str:
    original = without_fragment(original) if original else None
    target = without_fragment(target) if target else None
    for row in rows:
        requested = without_fragment(row["requested_url"])
        aliases = {requested}
        if row.get("final_url"):
            aliases.add(without_fragment(row["final_url"]))
        if target:
            if target != requested:
                continue
            if original and original not in aliases:
                raise ValueError("ファイル内の元URLがこの行のURLと異なります")
        elif original not in aliases:
            continue
        return row["requested_url"]
    raise ValueError("元URLがない、または対象一覧にありません。対象URLの行へファイルをドロップしてください")


class ImportSession:
    def __init__(self, company_dir: Path = ROOT / "data" / "companies", rejected_dir: Path | None = None):
        self.company_dir = company_dir
        self.rejected_dir = rejected_dir
        self.seen: dict[str, dict] = {}
        self.imported: dict[str, dict] = {}

    def pending(self) -> list[dict]:
        rows = [entry for entry in pf.unresolved_failures() if needs_manual_save(entry)]
        self.seen.update({entry["requested_url"]: entry for entry in rows})
        return rows

    def company_name(self, company_id: str) -> str:
        if not re.fullmatch(r"\d{13}", company_id):
            return company_id or "法人不明"
        try:
            profile = json.loads((self.company_dir / f"{company_id}.json").read_text(encoding="utf-8"))
            return profile.get("legal_name") or company_id
        except (OSError, ValueError):
            return company_id

    def upload(self, name: str, body: bytes, modified: str, target: str | None = None) -> dict:
        rows = self.pending()
        original, payload, filename, stamp = saved_document(name, body)
        url = match_url(original, target, rows)
        if stamp is None:
            try:
                stamp = datetime.fromtimestamp(float(modified) / 1000, timezone.utc).isoformat(timespec="seconds")
            except (ValueError, OverflowError, OSError) as exc:
                raise ValueError("File.lastModified が不正です") from exc
        with tempfile.TemporaryDirectory(prefix="survey-import-") as td:
            path = Path(td) / filename
            path.write_bytes(payload)
            result = pf.import_snapshot(url, path, stamp)
        self.imported[url] = next(row for row in rows if row["requested_url"] == url)
        return result

    def reject(self, name: str, target: str | None, body: bytes, error: str):
        """Report a rejected upload and keep its bytes locally so the reason can be checked."""
        saved = ""
        if self.rejected_dir and body:
            self.rejected_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]", "_", Path(name).name)[-120:] or "upload"
            path = self.rejected_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}_{safe}"
            path.write_bytes(body)
            saved = f" saved={path}"
        print(f"rejected file={name!r} target={target!r}: {error}{saved}", file=sys.stderr, flush=True)

    def revise_commands(self) -> str:
        groups: dict[tuple[int, str], dict[str, str]] = {}
        missing = False
        for row in self.imported.values():
            company_id, year = row.get("company_id"), row.get("recruitment_year")
            job = row.get("job_name", "")
            phase = "profiles" if job.startswith("company_profile") else "cases" if job.startswith("recruitment_case") else None
            if not re.fullmatch(r"\d{13}", str(company_id or "")) or not isinstance(year, int) or not phase:
                missing = True
                continue
            groups.setdefault((int(year), phase), {})[company_id] = self.company_name(company_id)
        commands = []
        for (year, phase), companies in sorted(groups.items()):
            path = f"tmp/page_snapshot_revise_{year}_{phase}.json"
            content = json.dumps({"companies": [{"company_id": cid, "legal_name": name}
                                                for cid, name in sorted(companies.items())]}, ensure_ascii=False)
            commands.append(f"mkdir -p tmp\nprintf '%s\\n' {shlex.quote(content)} > {shlex.quote(path)}\n"
                            f"python3 scripts/survey_companies.py {shlex.quote(path)} {year} --revise "
                            f"--phase {phase} --company-ids {' '.join(shlex.quote(cid) for cid in sorted(companies))}")
        if missing:
            commands.append("# 法人番号・年度・job_name が不明なインポートはコマンドを生成できません。")
        return "\n\n".join(commands) or "このサーバー起動後にインポートした企業はまだありません。"

    def page(self) -> str:
        pending = {row["requested_url"] for row in self.pending()}
        groups: dict[str, list[dict]] = {}
        for row in self.seen.values():
            groups.setdefault(str(row.get("company_id") or ""), []).append(row)
        tables = []
        for cid, rows in sorted(groups.items(), key=lambda item: self.company_name(item[0])):
            tables.append(f"<h2>{html.escape(self.company_name(cid))} ({html.escape(cid)})</h2><table>")
            for row in rows:
                url = row["requested_url"]
                escaped = html.escape(url, quote=True)
                done = url not in pending
                tables.append(f'<tr data-url="{escaped}"><td>{html.escape(self.company_name(cid))}</td>'
                              f'<td>{html.escape(str(row.get("recruitment_year") or "—"))}</td>'
                              f'<td><a href="{escaped}" target="_blank" rel="noopener noreferrer">{escaped}</a></td>'
                              f'<td>{html.escape(row.get("reason", ""))}</td><td>{"完了" if done else "未インポート"}</td>'
                              f'<td><input type="file" multiple {"disabled" if done else ""}></td></tr>')
            tables.append("</table>")
        return PAGE.replace("__ROWS__", "".join(tables)).replace("__COMMANDS__", html.escape(self.revise_commands()))


PAGE = """<!doctype html><html lang="ja"><meta charset="utf-8">
<title>保存ページのインポート</title>
<style>body{font-family:sans-serif;margin:2em}table{border-collapse:collapse;width:100%}td{border:1px solid #ccc;padding:.5em;overflow-wrap:anywhere}#drop{border:3px dashed #789;padding:2em}pre{white-space:pre-wrap}tr.hover,#drop.hover{background:#def}</style>
<h1>保存ページのインポート</h1>
<p>対象ページを自分のブラウザーで開き、HTML / MHTML / PDF / TXTとして保存してください。ログイン・CAPTCHA画面は取り込めません。</p>
<div id="drop">ここへ複数ファイルをドロップ（元URLを自動照合） <input id="files" type="file" multiple></div>
<p>元URLがないPDFやFirefox保存は、対象URLの行へドロップするか、その行のファイル選択を使ってください。</p>
<button onclick="location.reload()">一覧・再調査コマンドを更新</button><pre id="results" aria-live="polite"></pre>
__ROWS__
<h2>このセッションでインポートした企業だけを再調査</h2>
<p>以下は表示のみです。このworkspaceのルートで実行してください。再調査は自動実行しません。</p>
<pre id="commands">__COMMANDS__</pre>
<script>
const results=document.querySelector('#results');
async function upload(files,target){
 for(const file of files){
  try{
   const response=await fetch('upload',{method:'POST',headers:{
    'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name),
    'X-Last-Modified':String(file.lastModified),'X-Target-URL':encodeURIComponent(target||'')},body:file});
   const result=await response.json();
   results.textContent+=file.name+': '+(result.ok?'インポート済み':'拒否')+' '+(result.error||result.requested_url||'')+'\\n';
   if(result.ok){
    for(const row of document.querySelectorAll('tr[data-url]')){
     if(row.dataset.url===result.requested_url){row.cells[4].textContent='完了';row.querySelector('input').disabled=true;}
    }
    const updated=new DOMParser().parseFromString(await (await fetch('./')).text(),'text/html');
    document.querySelector('#commands').textContent=updated.querySelector('#commands').textContent;
   }
  }catch(error){results.textContent+=file.name+': 拒否 '+error.message+'\\n';}
 }
}
function zone(element,target){
 element.addEventListener('dragover',event=>{event.preventDefault();element.classList.add('hover');});
 element.addEventListener('dragleave',()=>element.classList.remove('hover'));
 element.addEventListener('drop',event=>{event.preventDefault();event.stopPropagation();element.classList.remove('hover');upload(event.dataTransfer.files,target);});
 element.querySelector('input').addEventListener('change',event=>upload(event.target.files,target));
}
zone(document.querySelector('#drop'),null);
for(const row of document.querySelectorAll('tr[data-url]')){
 if(!row.querySelector('input').disabled)zone(row,row.dataset.url);
}
document.addEventListener('dragover',event=>event.preventDefault());
document.addEventListener('drop',event=>event.preventDefault());
</script></html>"""


def make_server(host: str, port: int, token: str, session: ImportSession | None = None) -> HTTPServer:
    session = session or ImportSession()
    prefix = f"/{token}/"

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, format, *args):
            pass  # Do not leak the capability token into access logs.

        def respond(self, status: int, body: bytes, mime: str):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != prefix:
                self.respond(404, b"Not found", "text/plain")
                return
            self.respond(200, session.page().encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):
            if self.path != prefix + "upload":
                self.respond(404, b"Not found", "text/plain")
                return
            origin = self.headers.get("Origin")
            if (origin and origin != "http://" + self.headers.get("Host", "")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.respond(403, b"Forbidden", "text/plain")
                return
            name, body, target = "", b"", None
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("chunked uploads are not supported")
                name = unquote(self.headers.get("X-File-Name", ""))
                limit = upload_limit(name)
                size = int(self.headers.get("Content-Length", "-1"))
                if size < 0 or size > limit:
                    raise ValueError(f"saved page exceeds {limit // (1024 * 1024)} MiB or Content-Length is missing")
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError("incomplete upload")
                target = unquote(self.headers.get("X-Target-URL", "")) or None
                result = session.upload(name, body, self.headers.get("X-Last-Modified", ""), target)
                status = 200
            except (ValueError, OSError) as exc:
                result, status = {"ok": False, "error": str(exc)}, 400
                session.reject(name, target, body, str(exc))
            self.respond(status, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    return HTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="bind address, e.g. this machine's Tailscale IP")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-name", help="host name shown in the URL, e.g. the MagicDNS name")
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    session = ImportSession(rejected_dir=ROOT / "tmp" / "import_rejected")
    with make_server(args.host, args.port, token, session) as server:
        port = server.server_address[1]
        if args.public_name:
            print(f"http://{args.public_name}:{port}/{token}/", flush=True)
        print(f"IP fallback: http://{args.host}:{port}/{token}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
