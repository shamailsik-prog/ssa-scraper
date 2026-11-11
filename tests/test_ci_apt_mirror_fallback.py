"""The Tesseract apt step must retry dead Azure DNS without weakening signatures or TLS."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_WORKFLOW = Path(".github/workflows/ci.yml")
_STEP = "      - name: System packages (Tesseract for OCR tests)\n"

_GOOD_SOURCES = """\
Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble noble-updates noble-backports noble-security
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Verify-Peer: yes
"""

_STUB = """\
sudo() {
  local assigns=()
  while [ "$#" -gt 0 ] && [[ "$1" == *=* ]]; do
    assigns+=("$1")
    shift
  done
  if [ "${#assigns[@]}" -gt 0 ]; then
    env "${assigns[@]}" "$@"
  else
    "$@"
  fi
}
getent() {
  local name="${2-}"
  echo "$name" >> "$DNS_LOG"
  local n
  n=$(wc -l < "$DNS_LOG")
  if [ "$n" -le "$DNS_COLD" ]; then
    return 2
  fi
  case ",${DNS_ALLOW}," in
    *,"$name",*) return 0 ;;
  esac
  return 2
}
sleep() { echo "$*" >> "$SLEEP_LOG"; }
apt-get() {
  printf '%s\\n' "$*" >> "$APT_LOG"
  local n
  n=$(wc -l < "$APT_LOG")
  case "$APT_MODE" in
    ok) return 0 ;;
    fail-then-dns-on-third)
      if [ "$n" -le 2 ]; then
        echo "Could not connect to archive" >&2
        return 100
      fi
      if [ "$n" -eq 3 ]; then
        echo "Temporary failure resolving 'azure.archive.ubuntu.com'" >&2
        return 100
      fi
      return 0
      ;;
    *)
      echo "unknown APT_MODE $APT_MODE" >&2
      return 99
      ;;
  esac
}
"""


def _workflow_script() -> str:
    text = _WORKFLOW.read_text(encoding="utf-8")
    start = text.index(_STEP)
    start = text.index("        run: |\n", start) + len("        run: |\n")
    end = text.index("\n      - name:", start)
    lines = []
    for line in text[start:end].splitlines():
        if line.startswith("          "):
            lines.append(line[10:])
        elif line.strip() == "":
            lines.append("")
        else:
            raise AssertionError(repr(line))
    return "\n".join(lines) + "\n"


def _rewriter_source() -> str:
    script = _workflow_script()
    start = script.index("<<'PY'\n") + len("<<'PY'\n")
    end = script.index("\nPY\n", start)
    return script[start:end] + "\n"


def _function_source(name: str) -> str:
    script = _workflow_script()
    marker = f"{name}() {{"
    start = script.index(marker)
    depth = 0
    for index, char in enumerate(script[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1] + "\n"
    raise AssertionError(name)


def _detail(completed: subprocess.CompletedProcess[str]) -> str:
    return f"exit={completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"


def _seed_apt_root(root: Path, sources: str) -> None:
    (root / "sources.list.d").mkdir(parents=True, exist_ok=True)
    (root / "sources.list.d" / "ubuntu.sources").write_text(sources, encoding="utf-8")
    (root / "apt-mirrors.txt").write_text(
        "https://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n",
        encoding="utf-8",
    )


def _run_rewriter(tmp_path: Path, archive: str, security: str, sources: str, sources_list: str | None = None):
    root = tmp_path / "apt"
    _seed_apt_root(root, sources)
    if sources_list is not None:
        (root / "sources.list").write_text(sources_list, encoding="utf-8")
    program = tmp_path / "rewrite.py"
    program.write_text(_rewriter_source(), encoding="utf-8")
    completed = subprocess.run(
        ["/usr/bin/python3", str(program)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "ARCHIVE_URI": archive, "SECURITY_URI": security, "APT_ROOT": str(root)},
    )
    return completed, root


def _run_step(
    tmp_path: Path,
    *,
    dns_cold: int,
    dns_allow: str,
    apt_mode: str,
    sources: str = _GOOD_SOURCES,
) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "apt"
    _seed_apt_root(root, sources)
    wrapper = tmp_path / "step.sh"
    wrapper.write_text(_STUB + _workflow_script(), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "APT_ROOT": str(root),
            "DNS_LOG": str(tmp_path / "dns.log"),
            "DNS_COLD": str(dns_cold),
            "DNS_ALLOW": dns_allow,
            "APT_LOG": str(tmp_path / "apt.log"),
            "APT_MODE": apt_mode,
            "SLEEP_LOG": str(tmp_path / "sleep.log"),
        }
    )
    return subprocess.run(["bash", str(wrapper)], cwd=tmp_path, capture_output=True, text=True, env=env)


def test_tesseract_step_is_valid_bash():
    script = _workflow_script()
    completed = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert completed.returncode == 0, _detail(completed)
    assert 'case "${archive}${security}"' not in script
    assert 'require_https_mirror "$archive"' in script
    assert 'require_https_mirror "$security"' in script


def test_workflow_keeps_signature_and_tls_checks():
    text = _WORKFLOW.read_text(encoding="utf-8").lower()
    assert "verify=false" not in text
    assert "verify-peer=false" not in text
    assert "verify-peer=no" not in text
    assert "--allow-unauthenticated" not in text
    assert "--allow-insecure" not in text
    assert "[trusted=yes]" not in text
    assert "acquire::https::verify-peer=true" in text
    assert "acquire::http::timeout=120" in text
    assert "acquire::https::timeout=120" in text


def test_require_https_mirror_checks_each_uri():
    function = _function_source("require_https_mirror")

    def invoke(uri: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", function + '\nrequire_https_mirror "$1"\n', "mirror", uri],
            capture_output=True,
            text=True,
        )

    ok = invoke("https://archive.ubuntu.com/ubuntu/")
    assert ok.returncode == 0, _detail(ok)
    insecure = invoke("http://security.ubuntu.com/ubuntu/")
    assert insecure.returncode == 1
    assert "refusing non-HTTPS apt mirror" in insecure.stderr
    azure = invoke("https://azure.archive.ubuntu.com/ubuntu/")
    assert azure.returncode == 1
    assert "refusing Azure apt mirror" in azure.stderr


def test_rewriter_keeps_signed_by_and_verify_peer_yes(tmp_path):
    completed, root = _run_rewriter(
        tmp_path,
        "https://archive.ubuntu.com/ubuntu/",
        "https://security.ubuntu.com/ubuntu/",
        _GOOD_SOURCES,
        sources_list=(
            "deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] "
            "mirror+file:/etc/apt/apt-mirrors.txt noble main\n"
        ),
    )
    assert completed.returncode == 0, _detail(completed)
    sources = (root / "sources.list.d" / "ubuntu.sources").read_text(encoding="utf-8")
    assert sources.count("Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg") == 2
    assert sources.count("Verify-Peer: yes") == 2
    assert "URIs: https://archive.ubuntu.com/ubuntu/" in sources
    assert "URIs: https://security.ubuntu.com/ubuntu/" in sources
    assert "noble-security" in sources
    assert "azure.archive.ubuntu.com" not in sources
    assert "mirror+file:" not in sources
    assert "trusted=yes" not in sources.lower()
    mirrors = (root / "apt-mirrors.txt").read_text(encoding="utf-8")
    assert mirrors.startswith("https://archive.ubuntu.com/ubuntu/")
    assert "azure.archive.ubuntu.com" not in mirrors
    listing = (root / "sources.list").read_text(encoding="utf-8")
    assert listing.startswith("deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] https://")
    assert "trusted=yes" not in listing.lower()


def test_rewriter_rejects_weakened_trust_and_non_https(tmp_path):
    cases = [
        ("https://archive.ubuntu.com/ubuntu/", "http://security.ubuntu.com/ubuntu/", _GOOD_SOURCES, None),
        ("https://azure.archive.ubuntu.com/ubuntu/", "https://security.ubuntu.com/ubuntu/", _GOOD_SOURCES, None),
        ("http://archive.ubuntu.com/ubuntu/", "https://security.ubuntu.com/ubuntu/", _GOOD_SOURCES, None),
        (
            "https://archive.ubuntu.com/ubuntu/",
            "https://security.ubuntu.com/ubuntu/",
            _GOOD_SOURCES.replace("Verify-Peer: yes", "Verify-Peer: no"),
            None,
        ),
        (
            "https://archive.ubuntu.com/ubuntu/",
            "https://security.ubuntu.com/ubuntu/",
            _GOOD_SOURCES + "Trusted: yes\n",
            None,
        ),
        (
            "https://archive.ubuntu.com/ubuntu/",
            "https://security.ubuntu.com/ubuntu/",
            _GOOD_SOURCES,
            "deb [trusted=yes] mirror+file:/etc/apt/apt-mirrors.txt noble main\n",
        ),
        (
            "https://archive.ubuntu.com/ubuntu/",
            "https://security.ubuntu.com/ubuntu/",
            _GOOD_SOURCES,
            "deb [verify-peer=false] mirror+file:/etc/apt/apt-mirrors.txt noble main\n",
        ),
    ]
    for archive, security, sources, listing in cases:
        completed, _root = _run_rewriter(tmp_path / archive.split("/")[2], archive, security, sources, listing)
        assert completed.returncode != 0, (archive, security, _detail(completed))
        assert "refusing" in completed.stderr.lower(), _detail(completed)


def test_dns_miss_retries_inside_the_loop_then_installs(tmp_path):
    completed = _run_step(
        tmp_path,
        dns_cold=4,
        dns_allow="archive.ubuntu.com,security.ubuntu.com",
        apt_mode="ok",
    )
    assert completed.returncode == 0, _detail(completed)
    assert "apt attempt 1/3 skipped; no official Ubuntu mirror resolved yet" in completed.stdout
    assert "official Ubuntu mirrors did not resolve; retrying DNS inside the install loop" in completed.stderr
    assert "installed tesseract-ocr and fonts-dejavu-core" in completed.stdout
    assert "Acquire::https::Verify-Peer=true" in (tmp_path / "apt.log").read_text(encoding="utf-8")
    sources = (tmp_path / "apt" / "sources.list.d" / "ubuntu.sources").read_text(encoding="utf-8")
    assert "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg" in sources
    assert "Verify-Peer: yes" in sources
    assert "https://archive.ubuntu.com/ubuntu/" in sources


def test_total_dns_failure_retries_then_exits(tmp_path):
    completed = _run_step(tmp_path, dns_cold=1000, dns_allow="", apt_mode="ok")
    assert completed.returncode == 1, _detail(completed)
    assert "apt attempt 1/3 skipped" in completed.stdout
    assert "apt attempt 3/3 skipped" in completed.stdout
    assert "failed to install tesseract-ocr and fonts-dejavu-core after 3 attempts" in completed.stderr
    assert not (tmp_path / "apt.log").exists()
    sleeps = (tmp_path / "sleep.log").read_text(encoding="utf-8").split()
    assert sleeps == ["15", "30"]


def test_validation_failure_is_fatal(tmp_path):
    completed = _run_step(
        tmp_path,
        dns_cold=0,
        dns_allow="archive.ubuntu.com,security.ubuntu.com",
        apt_mode="ok",
        sources=_GOOD_SOURCES.replace("Verify-Peer: yes", "Verify-Peer: no"),
    )
    assert completed.returncode == 1, _detail(completed)
    assert "refusing apt source that weakens signature or TLS checks" in completed.stderr
    assert "apt source rewrite rejected the sources" in completed.stderr
    assert "apt attempt 2" not in completed.stdout
    assert "installed tesseract-ocr" not in completed.stdout
    assert not (tmp_path / "apt.log").exists()


def test_last_attempt_rewrite_reserves_one_install(tmp_path):
    completed = _run_step(
        tmp_path,
        dns_cold=0,
        dns_allow="azure.archive.ubuntu.com,archive.ubuntu.com,security.ubuntu.com",
        apt_mode="fail-then-dns-on-third",
    )
    assert completed.returncode == 0, _detail(completed)
    assert "rewrote apt sources on the final attempt; reserving one install against the new mirrors" in completed.stdout
    assert "apt attempt 4/4" in completed.stdout
    assert "installed tesseract-ocr and fonts-dejavu-core" in completed.stdout
    apt_log = (tmp_path / "apt.log").read_text(encoding="utf-8")
    assert apt_log.count("Acquire::https::Verify-Peer=true") >= 2
    assert "Verify-Peer=false" not in apt_log
    assert "trusted=yes" not in apt_log.lower()
    sources = (tmp_path / "apt" / "sources.list.d" / "ubuntu.sources").read_text(encoding="utf-8")
    assert sources.count("Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg") == 2
    assert "Verify-Peer: yes" in sources
    assert "azure.archive.ubuntu.com" not in sources
    assert "mirror+file:" not in sources


def test_security_mirror_falls_back_without_dropping_signed_by(tmp_path):
    completed = _run_step(
        tmp_path,
        dns_cold=0,
        dns_allow="archive.ubuntu.com",
        apt_mode="ok",
    )
    assert completed.returncode == 0, _detail(completed)
    assert "security.ubuntu.com does not resolve; security suites use https://archive.ubuntu.com/ubuntu/" in completed.stdout
    sources = (tmp_path / "apt" / "sources.list.d" / "ubuntu.sources").read_text(encoding="utf-8")
    assert "https://security.ubuntu.com" not in sources
    assert sources.count("URIs: https://archive.ubuntu.com/ubuntu/") == 2
    assert "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg" in sources
    assert "http://" not in sources
