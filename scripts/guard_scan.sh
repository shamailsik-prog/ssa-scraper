#!/usr/bin/env bash
# Repository guard scan (Amendment §25 step 17). Fails on:
#   * TODO / FIXME / XXX / placeholder stubs in service code
#   * `pass` used as a function body stub
#   * credential-shaped literals
#   * forbidden bypass logic: CAPTCHA solvers, proxy pools, stealth, automatic slot rotation past a block
#   * managed ScrapeGraph use from login_session code paths
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0
scan() { # $1 = label, $2 = regex, $3.. = paths
  local label="$1" re="$2"; shift 2
  if grep -rInE --include='*.py' --include='*.html' --include='*.yml' --include='*.yaml' "$re" "$@" ; then
    echo "GUARD FAIL: $label"; fail=1
  fi
}
scan "TODO/FIXME stubs" '\b(TODO|FIXME|XXX|HACK|NotImplemented\(\)|raise NotImplementedError\(\"stub)' scraper migrations
if grep -rInE --include='*.py' '\bplaceholder\b' scraper migrations; then echo 'GUARD FAIL: placeholder text'; fail=1; fi
scan "pass-stub bodies" '^\s*def [a-zA-Z_]+\(.*\):\s*$' scraper | true
if grep -rInE --include='*.py' -B1 '^\s+pass\s*$' scraper migrations | grep -E 'def .*:\s*$' ; then echo "GUARD FAIL: pass-stub function body"; fail=1; fi
scan "hard-coded API keys" '(sgai-[A-Za-z0-9-]{16,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|gAAAA[A-Za-z0-9_-]{40,})' scraper migrations docker-compose.yml Dockerfile
scan "password literals" '(PASSWORD|PASS|TOKEN|SECRET)[A-Z_]*\s*=\s*"[^"]{8,}"' scraper migrations
scan "CAPTCHA solver" '(2captcha|anticaptcha|anti-captcha|capsolver|solve_captcha|captcha_solver|deathbycaptcha)' scraper
scan "proxy pool / stealth evasion" '(proxy_pool|rotate_proxy|playwright_stealth|puppeteer-extra-plugin-stealth|undetected_chromedriver|stealth=True)' scraper
scan "automatic recovery to primary slot" 'try_recover_primary' scraper
if grep -rInE --include='*.py' 'ManagedScrapeGraphEngine|smartscraper\(' scraper/auth scraper/tasks/pakistanlawsite.py scraper/tasks/search_map.py ; then echo "GUARD FAIL: managed ScrapeGraph referenced from a login_session code path"; fail=1; fi
if [ -f .env ]; then echo "note: .env present locally (ignored by git)"; fi
if git ls-files | grep -qE '(^|/)\.env$'; then echo "GUARD FAIL: .env is tracked"; fail=1; fi
if [ "$fail" -ne 0 ]; then exit 1; fi
echo "guard scan: clean"
