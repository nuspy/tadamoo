"""What a shell command does about the network, so that one which sends something out of this PC is told to the security monitor and
goes through the Rule of Two like any other outward action. A copy of canary/shellscan.py of the Openweight Canary (the independent
project): tests/test_shell_egress.py fails when the two bodies differ. The text is only analysed, never run."""
from __future__ import annotations

import base64
import binascii
import re

URL_RE = re.compile(r"(?i)\b(?:https?|ftp|wss?|s3|gs|oss|cos)://[^\s<>\"'`)\]}]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOSTISH_RE = re.compile(r"(?i)^(?:[a-z0-9-]+\.)+[a-z]{2,24}(?::\d{2,5})?(?:/\S*)?$")
FILE_LIKE = {"py", "sh", "md", "rs", "pl", "cc", "so", "ps", "js", "ts", "json", "txt", "log", "bin", "sql", "yml", "yaml", "toml", "cfg",
             "ini", "csv", "tsv", "xml", "html", "css", "c", "h", "cpp", "go", "rb", "php", "exe", "dll", "bat", "cmd", "ps1", "zip", "gz", "tar",
             "tgz", "png", "jpg", "jpeg", "gif", "pdf", "doc", "docx", "xlsx", "pem", "key", "crt", "out", "tmp", "bak", "env", "lock"}

# endpoints by what they are
ENDPOINTS = {
    "object_storage": r"(?:\.|^)(?:aliyuncs\.com|amazonaws\.com|blob\.core\.windows\.net|storage\.googleapis\.com|myqcloud\.com|bcebos\.com|volces\.com|"
                      r"myhuaweicloud\.com|obs\.[a-z0-9-]+\.[a-z.]+|ksyuncs\.com|qiniup\.com|qbox\.me|qiniudn\.com|upaiyun\.com|ufileos\.com|jdcloud-oss\.com|"
                      r"cloudflarestorage\.com|backblazeb2\.com|digitaloceanspaces\.com|wasabisys\.com|linodeobjects\.com|scw\.cloud|"
                      r"objectstorage\.[a-z0-9-]+\.oraclecloud\.com|dream\.io|r2\.dev|b-cdn\.net|supabase\.co|firebasestorage\.app|firebaseio\.com)$",
    "file_drop": r"(?:\.|^)(?:pastebin\.com|transfer\.sh|0x0\.st|file\.io|anonfiles\.com|gofile\.io|catbox\.moe|temp\.sh|dpaste\.[a-z]+|hastebin\.com|"
                 r"ghostbin\.[a-z]+|termbin\.com|ix\.io|sprunge\.us|paste\.[a-z]+|bashupload\.com|wetransfer\.com|send\.vis\.ee|tmpfiles\.org|"
                 r"gist\.github\.com|gist\.githubusercontent\.com)$",
    "callback_service": r"(?:\.|^)(?:webhook\.site|requestbin\.[a-z]+|pipedream\.net|interact\.sh|oast\.[a-z]+|burpcollaborator\.net|ngrok\.io|"
                        r"ngrok-free\.app|ngrok\.app|trycloudflare\.com|loca\.lt|serveo\.net|requestcatcher\.com|canarytokens\.com|dnslog\.cn|ceye\.io|"
                        r"beeceptor\.com|mockbin\.io|postb\.in|hookbin\.com|webhook\.[a-z]+)$",
    "messaging_hook": r"(?:\.|^)(?:discord(?:app)?\.com|api\.telegram\.org|hooks\.slack\.com|ntfy\.sh|api\.pushover\.net|maker\.ifttt\.com|"
                      r"hooks\.zapier\.com|open\.feishu\.cn|oapi\.dingtalk\.com|qyapi\.weixin\.qq\.com)$",
    "address_lookup": r"(?:\.|^)(?:ifconfig\.(?:me|co|io)|ipify\.org|icanhazip\.com|ipinfo\.io|checkip\.amazonaws\.com|ip-api\.com|ident\.me|"
                      r"wtfismyip\.com|myip\.com|myexternalip\.com|ipecho\.net|ipapi\.co|api\.my-ip\.io|whatismyip\.[a-z]+|ip\.sb|httpbin\.org|"
                      r"myip\.opendns\.com|ipconfig\.io)$",
}
ENDPOINT_RES = {k: re.compile(v, re.I) for k, v in ENDPOINTS.items()}

NET_TOOLS = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "tftp", "telnet", "socat", "iwr", "irm",
             "invoke-webrequest", "invoke-restmethod", "bitsadmin", "certutil", "aria2c", "httpie", "http", "https", "lynx", "w3m", "gh", "aws",
             "gsutil", "azcopy", "rclone", "ossutil", "ossutil64", "coscli", "mc", "s3cmd", "s5cmd", "obsutil", "bcecmd", "qshell", "git-lfs"}
DNS_TOOLS = {"nslookup", "dig", "host", "resolve-dnsname", "drill", "getent"}
PING_TOOLS = {"ping", "traceroute", "tracert", "tracepath", "mtr", "test-netconnection", "tnc", "pathping"}
LOOKUP_WORDS = {"which", "type", "where", "whereis", "get-command", "gcm", "hash"}
CAPABILITY_NAMES = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "socat", "python", "python3", "py",
                    "node", "perl", "ruby", "php", "jq", "openssl", "aria2c", "powershell", "pwsh", "git", "aws", "gsutil", "rclone"}
NET_CODE_RE = re.compile(r"(?i)\b(?:urllib|requests\.\w+|httpx\.\w+|aiohttp|http\.client|socket\.|fetch\(|XMLHttpRequest|net\.Socket|WebClient|"
                         r"HttpClient|Net\.Sockets|urlopen|Invoke-WebRequest|Invoke-RestMethod|\biwr\b|\birm\b)|/dev/(?:tcp|udp)/")
UPLOAD_CODE_RE = re.compile(r"(?i)(?:requests|httpx|session|client|http)\.(?:put|post|patch)\(|urlopen\([^)]*data\s*=|\.upload_file\(|\.put_object\(|"
                            r"-Method\s+['\"]?(?:Put|Post|Patch)\b|-InFile\b|WebClient[^\n]*Upload(?:File|String|Data)|\.send(?:all)?\(")
CURL_UPLOAD_FLAG = re.compile(r"^(?:-X|--request)$|^(?:-T|--upload-file|-d|--data|--data-raw|--data-binary|--data-urlencode|--data-ascii|-F|--form|"
                              r"--form-string|--json)(?:=.*)?$|^-[A-Za-z]*[dFT]$")
WGET_UPLOAD_FLAG = re.compile(r"^--(?:post-data|post-file|body-data|body-file)(?:=.*)?$|^--method=(?:PUT|POST|PATCH)$", re.I)
SYNC_TOOLS = {"aws", "gsutil", "azcopy", "rclone", "ossutil", "ossutil64", "coscli", "s3cmd", "s5cmd", "obsutil", "bcecmd", "qshell", "mc"}
SYNC_VERBS = {"cp", "copy", "sync", "mv", "move", "put", "upload", "push", "rsync", "send"}
READ_NETCONF_RE = re.compile(r"(?i)/etc/(?:resolv\.conf|hosts|nsswitch\.conf|network|netplan|NetworkManager|wpa_supplicant)|/proc/net/\w+|"
                             r"resolvectl|scutil\s+--dns|ipconfig\s+/all|Get-DnsClient\w*|Get-NetIPConfiguration|Get-NetRoute|route\s+print|"
                             r"netsh\s+(?:interface|winhttp|wlan)|\b(?:printenv|env|set|export)\b[^\n|;&]*\|\s*(?:grep|findstr|select-string)\b[^\n]*proxy|"
                             r"\b(?:http|https|no|all|ftp)_proxy\b|/etc/environment|apt\.conf|\.curlrc|\.wgetrc|\.npmrc|pip\.conf")
ADDRESS_CMD_RE = re.compile(r"(?i)\b(?:ip\s+(?:route|addr|address|a|r|link|neigh)|ifconfig|hostname\s+-[iI]|arp\s+-a|netstat\s+-[a-z]*r|"
                            r"ss\s+-[a-z]*|nmcli|networksetup|Get-NetIPAddress)\b")
OBFUSCATION_RE = re.compile(r"(?i)\|\s*(?:base64|b64|basenc|openssl\s+enc)\s+(?:-d|--decode|-D)\b[^\n]*\|\s*(?:ba|z|da)?sh\b|\beval\b|"
                            r"\b(?:iex|invoke-expression)\b|powershell[^\n]*\s-(?:e|enc|encodedcommand)\b|xxd\s+-r\s+-p[^\n]*\|\s*(?:ba)?sh|"
                            r"\\x[0-9a-f]{2}(?:\\x[0-9a-f]{2}){3,}|\$'\\x[0-9a-f]{2}|printf\s+['\"](?:\\x[0-9a-f]{2}){3,}|"
                            r"(?:chr\(\d+\)\s*\+\s*){3,}|\bfromCharCode\b|\|\s*rev\s*\||\bbase64\.b64decode\b[^\n]*\bexec\b")
DNS_SUBST_RE = re.compile(r"(?i)\b(?:nslookup|dig|host|resolve-dnsname|drill)\b[^\n;|&]*(?:\$\(|`|\$\{)[^\n]*\.[a-z0-9-]{2,}")
MARKER_RE = re.compile(r"(?i)\b(?:PROBE|CANARY|PAYLOAD|EXFIL|TOKEN|SECRET|MARKER)[_-][A-Za-z0-9_-]{3,}\b|\b(?:put_probe|test\.bin|probe\.(?:bin|txt)|"
                       r"upload_test|egress_test)\b")
CONTINUATION_RE = re.compile(r"[\\`][ \t]*\r?\n[ \t]*")        # a line that goes on in the next one (backslash in a shell, backtick in PowerShell)
SHELL_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|\||\n|`|\$\()\s*")
BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
HEX_ESCAPES_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2}){4,}")
DEFANG_SUBS = ((re.compile(r"(?i)\bh(?:xx|\*\*|tt)p(s?)\s*(?:\[:\]|:)\s*(?:\[?//\]?|\[//\])"), r"http\1://"),
               (re.compile(r"(?i)\bh(?:xx|\*\*)p(s?)://"), r"http\1://"),
               (re.compile(r"\[\s*\.\s*\]|\(\s*\.\s*\)|\{\s*\.\s*\}|\[\s*dot\s*\]|\(\s*dot\s*\)"), "."),
               (re.compile(r"\[\s*:\s*\]"), ":"), (re.compile(r"\[\s*/\s*\]"), "/"), (re.compile(r"\[\s*@\s*\]|\[\s*at\s*\]"), "@"))


def refang(text: str) -> str:
    """The text with the usual ways of writing a defanged address (hxxp, [.], [:]) put back, so that an address is seen as one."""
    for pattern, repl in DEFANG_SUBS:
        text = pattern.sub(repl, text)
    return text


def host_of(url: str) -> str:
    rest = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    rest = rest.rsplit("@", 1)[-1]
    return (rest.split(":", 1)[0] if not rest.startswith("[") else rest.strip("[]").split("]")[0]).lower().strip(".*")


def endpoint_class(host: str) -> str:
    host = host.lower().strip(".*")
    for name, pattern in ENDPOINT_RES.items():
        if pattern.search(host):
            return name
    return ""


def _decoded_views(command: str) -> list[str]:
    """What the command says once its encoded parts are read: base64 blobs, \\x escapes, a PowerShell -EncodedCommand."""
    views: list[str] = []
    for blob in set(BASE64_BLOB_RE.findall(command)):
        pad = blob + "=" * (-len(blob) % 4)
        try:
            raw = base64.b64decode(pad, validate=True)
        except (binascii.Error, ValueError):
            continue
        for enc in ("utf-8", "utf-16-le"):
            text = raw.decode(enc, errors="replace")
            if text and sum(c.isprintable() or c in "\r\n\t" for c in text) / len(text) > 0.9 and len(text) >= 8:
                views.append(text)
                break
    for run in set(HEX_ESCAPES_RE.findall(command)):
        try:
            text = bytes(int(h, 16) for h in re.findall(r"\\x([0-9a-fA-F]{2})", run)).decode("utf-8", errors="replace")
        except ValueError:
            continue
        if text.isprintable():
            views.append(text)
    return views


def _segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    command = CONTINUATION_RE.sub(" ", command)
    for seg in SHELL_SPLIT_RE.split(command):
        toks = seg.strip().split()
        while toks and (toks[0] in ("sudo", "env", "time", "nohup", "command", "exec", "timeout") or re.match(r"^[A-Za-z_]\w*=", toks[0])):
            if toks[0] == "command" and len(toks) > 1 and toks[1] in ("-v", "-V"):
                toks = ["type"] + toks[2:]
                break
            toks = toks[1:]
        if toks:
            out.append(toks)
    return out


def _first(tok: str) -> str:
    return re.sub(r"\.(exe|cmd|bat|ps1)$", "", tok.replace("\\", "/").split("/")[-1].lower())


def _host_tokens(tokens: list[str]) -> list[str]:
    """Hosts named by the arguments of a network command, with or without a scheme (an address in a URL, host:port, a bare name, an IPv4)."""
    hosts: list[str] = []
    for tok in tokens:
        t = tok.strip("\"'(),;")
        if t.startswith("-") or not t:
            continue
        if "://" in t:
            h = host_of(t)
        elif HOSTISH_RE.match(t) and t.split("/")[0].rsplit(".", 1)[-1].split(":")[0].lower() not in FILE_LIKE:
            h = host_of(t)
        elif IPV4_RE.fullmatch(t.split("/")[0].split(":")[0]):
            h = t.split("/")[0].split(":")[0]
        elif re.match(r"^[\w.-]+@[\w.-]+:", t):
            h = t.split("@", 1)[1].split(":", 1)[0].lower()
        else:
            continue
        if h and h not in hosts:
            hosts.append(h)
    return hosts


def analyse(command: str, known_hosts: set[str] | None = None, _depth: int = 0) -> dict:
    """What one command does about the network.

    {"reach": bool, "upload": bool, "dns": bool, "hosts": [...], "endpoints": {host: class}, "probe": [...], "obfuscated": bool, "markers": [...],
     "evidence": {...}}. `probe` holds "capability", "resolver", "address", "lookup", "connectivity". Encoded parts are read and analysed too."""
    text = refang(command)
    known = {h.lower() for h in (known_hosts or ())}
    res = {"reach": False, "upload": False, "dns": False, "hosts": [], "endpoints": {}, "probe": [], "obfuscated": False,
           "markers": MARKER_RE.findall(text)[:5], "evidence": {}}

    def probe(kind: str) -> None:
        if kind not in res["probe"]:
            res["probe"].append(kind)

    for toks in _segments(text):
        first = _first(toks[0])
        rest = toks[1:]
        low = [t.lower() for t in rest]
        if first in NET_TOOLS or first in PING_TOOLS:
            res["reach"] = True
            for h in _host_tokens(rest):
                if h not in res["hosts"]:
                    res["hosts"].append(h)
            if first == "curl":
                flags = [t for t in rest if t.startswith("-")]
                verbs = [low[i + 1] for i, t in enumerate(low[:-1]) if t in ("-x", "--request")] + [t.split("=", 1)[1] for t in low if t.startswith("--request=")]
                if any(v.upper() in ("PUT", "POST", "PATCH") for v in verbs) or any(CURL_UPLOAD_FLAG.match(f) and f not in ("-X", "--request") for f in flags):
                    res["upload"] = True
                    res["evidence"]["upload_flags"] = sorted({f.split("=")[0] for f in flags if CURL_UPLOAD_FLAG.match(f)} | {"-X " + v.upper() for v in verbs})[:6]
                short = any(re.match(r"^(?:-m|--max-time|--connect-timeout)(?:=.*)?$", f) for f in flags)
                shows_code = any(re.match(r"^(?:-w|--write-out)(?:=.*)?$", f) for f in flags) or "http_code" in text.lower()
                letters = {c for f in flags if f.startswith("-") and not f.startswith("--") for c in f[1:]}
                short = short or "m" in letters
                headonly = "I" in letters or "--head" in flags or ("-o" in rest and any(x in ("/dev/null", "nul") for x in rest))
                if short and (shows_code or headonly):
                    probe("connectivity")
            elif first == "wget":
                if any(WGET_UPLOAD_FLAG.match(t) for t in rest):
                    res["upload"] = True
                if "--spider" in low:
                    probe("connectivity")
            elif first in ("iwr", "irm", "invoke-webrequest", "invoke-restmethod"):
                if any(t in ("-infile", "-body") for t in low) or any(low[i + 1] in ("put", "post", "patch") for i, t in enumerate(low[:-1]) if t == "-method"):
                    res["upload"] = True
            elif first in ("scp", "rsync", "sftp") and any(re.match(r"^[\w.-]+@?[\w.-]+:", t) for t in rest):
                res["upload"] = True
            elif first in SYNC_TOOLS and any(t in SYNC_VERBS for t in low[:3]):
                res["upload"] = True
            elif first in ("nc", "ncat", "netcat", "socat"):
                res["upload"] = res["upload"] or ("<" in text or "|" in command)
                if any(t.startswith("-z") or t in ("-zv", "-vz") for t in low):
                    probe("connectivity")
            elif first in PING_TOOLS or first in ("telnet",):
                probe("connectivity")
        elif first in DNS_TOOLS:
            res["dns"] = True
            for h in _host_tokens(rest):
                if h not in res["hosts"]:
                    res["hosts"].append(h)
            if any(endpoint_class(h) == "address_lookup" or h.startswith("myip") for h in res["hosts"]):
                probe("lookup")
        elif first in LOOKUP_WORDS:
            named = [t for t in low if t in CAPABILITY_NAMES]
            if len(named) >= 1 and any(t in ("curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "telnet", "socat", "aria2c") for t in named):
                probe("capability")
    if DNS_SUBST_RE.search(text):
        res["dns"] = True
        res["evidence"]["dns_with_substitution"] = True
    if NET_CODE_RE.search(text):
        res["reach"] = True
        if UPLOAD_CODE_RE.search(text):
            res["upload"] = True
        for url in URL_RE.findall(text):
            h = host_of(url)
            if h and h not in res["hosts"]:
                res["hosts"].append(h)
    for url in URL_RE.findall(text):
        h = host_of(url)
        if h and h not in res["hosts"]:
            res["hosts"].append(h)
    if READ_NETCONF_RE.search(text):
        probe("resolver")
    if ADDRESS_CMD_RE.search(text):
        probe("address")
    for h in res["hosts"]:
        cls = endpoint_class(h)
        if cls:
            res["endpoints"][h] = cls
            if cls == "address_lookup":
                probe("lookup")
    if OBFUSCATION_RE.search(text):
        res["obfuscated"] = True
    if _depth < 2:
        for view in _decoded_views(text):
            inner = analyse(view, known_hosts, _depth + 1)
            res["obfuscated"] = res["obfuscated"] or bool(inner["reach"] or inner["upload"] or inner["probe"])
            for key in ("reach", "upload", "dns"):
                res[key] = res[key] or inner[key]
            for h in inner["hosts"]:
                if h not in res["hosts"]:
                    res["hosts"].append(h)
            res["endpoints"].update(inner["endpoints"])
            for p in inner["probe"]:
                probe(p)
    res["foreign"] = [h for h in res["hosts"] if h not in known and not _is_local(h)]
    return res


def _is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost") or bool(re.match(r"^(?:127\.|10\.|192\.168\.|169\.254\.)", host))


def probe_then_send(history: list[dict], current: dict, window: int = 8) -> bool:
    """True when this command reaches out or uploads and, within the last `window` commands of the same session, one probed the machine's reach."""
    if not (current["reach"] or current["upload"]):
        return bool(current["probe"] and current["upload"])
    if current["probe"]:
        return True
    return any(h["probe"] for h in history[-window:])


def sends_out(arguments) -> bool:
    """Whether a shell command (the `command` of the call) sends or names something outside this PC: an upload, a host that is not local,
    a DNS name carrying data, or a hidden command that reaches out."""
    command = arguments.get("command") if isinstance(arguments, dict) else None
    if not isinstance(command, str) or not command.strip():
        return False
    found = analyse(command)
    return bool(found["upload"] or found["foreign"] or found["evidence"].get("dns_with_substitution") or (found["obfuscated"] and found["reach"]))
