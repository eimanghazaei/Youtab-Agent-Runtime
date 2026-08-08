#!/usr/bin/env bash
#
# Prove the origin's TLS floor against a real nginx, not against a string.
#
# `tests/youtab_runtime/test_origin_protection.py` reads the generated block
# and asserts what it says. That is necessary and it is not sufficient: the
# question this gate answers is whether an nginx built the way the deployment
# target is built actually refuses a TLS 1.0 ClientHello. Nothing about a
# config file guarantees that -- `ssl_protocols` absent was the entire
# exposure, and "absent" reads as innocuous in a diff.
#
# nginx 1.18.0 because that is what the origin runs (Ubuntu 20.04 LTS). The
# image is Debian Buster, so its OpenSSL is 1.1.1, which still speaks TLS 1.0
# and TLS 1.1. That matters more here than the nginx version does; see below.
#
#
# Why this gate cannot pass vacuously
# -----------------------------------
# "TLS 1.0 was rejected" is worthless on its own. A modern OpenSSL client
# refuses to *offer* TLS 1.0 no matter what the server would have accepted, so
# the assertion passes against a server that happily serves it. A test that
# cannot fail is not evidence.
#
# So this gate runs two servers from the SAME artifact:
#
#   :443   the committed file, byte for byte
#   :9443  the committed file with `ssl_protocols` mutated to re-enable
#          TLS 1.0 and TLS 1.1, and nothing else changed
#
# and it asserts, before it asserts anything else, that :9443 NEGOTIATES
# TLS 1.0 and TLS 1.1 with this client. If it does not, the environment cannot
# observe legacy TLS, every rejection below would be vacuous, and the gate
# fails saying exactly that rather than reporting a pass it did not earn.
#
# That preflight is also the mutation evidence, re-run on every CI run rather
# than performed once by hand: the only difference between the server that
# serves TLS 1.0 and the server that refuses it is the directive this change
# added.
#
# The client's OpenSSL is deliberately relaxed (`MinProtocol = None`,
# `SECLEVEL=0`) so that it will offer legacy protocols. That relaxation lives
# in this container and nowhere else. It can only make the gate harder to
# pass: it removes the client-side and library-side reasons a legacy handshake
# might fail, leaving `ssl_protocols` as the only thing left that can refuse
# it. Production configuration is never weakened to make a test observable.
set -uo pipefail

IMAGE="${YOUTAB_NGINX_IMAGE:-nginx:1.18.0}"
CERTS=/etc/ssl/cloudflare
CONF_SRC="infrastructure/nginx/agent.youtab.io.conf"

# ---------------------------------------------------------------------------
# Host side: bring our own nginx. There is none on the runner, and installing
# the distribution's would test whatever version that distribution ships.
# ---------------------------------------------------------------------------
if [ "${YOUTAB_TLS_GATE_IN_CONTAINER:-0}" != "1" ]; then
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  echo "== origin TLS gate: running inside ${IMAGE} =="
  # Carried in so the container can prove the file it tests is the file in the
  # repository, rather than something a mount or a line-ending translation
  # rewrote on the way in.
  conf_sha="$(sha256sum "${repo_root}/${CONF_SRC}" | cut -d' ' -f1)"
  exec docker run --rm \
    -v "${repo_root}:/repo:ro" \
    -e YOUTAB_TLS_GATE_IN_CONTAINER=1 \
    -e "YOUTAB_TLS_GATE_CONF_SHA256=${conf_sha}" \
    --entrypoint /bin/bash \
    "${IMAGE}" /repo/scripts/youtab/nginx_tls_gate.sh
fi

# ---------------------------------------------------------------------------
# Container side.
# ---------------------------------------------------------------------------
cd /repo || exit 1

failures=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
section() { printf '\n-- %s\n' "$1"; }

section "environment"
nginx -v 2>&1 | sed 's/^/  /'
nginx_version="$(nginx -v 2>&1 | sed 's/.*nginx\///')"
case "$nginx_version" in
  1.18.*) pass "nginx ${nginx_version} matches the deployment target series" ;;
  *) fail "expected nginx 1.18.x, got ${nginx_version}" ;;
esac

if ! command -v openssl >/dev/null 2>&1; then
  echo "  openssl CLI absent; installing it from the Debian archive"
  rm -f /etc/apt/sources.list.d/nginx.list
  sed -i 's|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g' \
    /etc/apt/sources.list
  echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq --no-install-recommends openssl >/dev/null 2>&1
fi
if ! command -v openssl >/dev/null 2>&1; then
  printf '  FAIL  no openssl CLI in %s and it could not be installed.\n' "$IMAGE"
  printf '        The handshake matrix cannot be produced, so this gate has\n'
  printf '        measured nothing. Not reporting a pass.\n'
  exit 1
fi
openssl version | sed 's/^/  /'

section "the artifact under test is the one in the repository"
installed_sha="$(sha256sum "$CONF_SRC" | cut -d' ' -f1)"
if [ "$installed_sha" = "${YOUTAB_TLS_GATE_CONF_SHA256:-}" ]; then
  pass "sha256 ${installed_sha} matches the file on the host"
else
  fail "sha256 mismatch: host ${YOUTAB_TLS_GATE_CONF_SHA256:-unset}, container ${installed_sha}"
  exit 1
fi
if grep -q $'\r' "$CONF_SRC"; then
  fail "the artifact carries CR bytes; it was checked out with CRLF line endings"
else
  pass "LF line endings survived checkout (the .gitattributes pin holds)"
fi

section "test certificates"
# Generated here, thrown away with the container. The origin-pull CA doubles
# as the issuer of the server certificate: nothing here is testing PKI, only
# whether `ssl_verify_client on` demands a certificate this CA signed.
mkdir -p "$CERTS"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -sha256 \
  -keyout "$CERTS/ca.key" -out "$CERTS/origin-pull-ca.pem" \
  -subj "/CN=Youtab test origin-pull CA" >/dev/null 2>&1 || exit 1
gen_leaf() {
  openssl req -newkey rsa:2048 -nodes -keyout "$CERTS/$1.key" -out "$CERTS/$1.csr" \
    -subj "/CN=$2" >/dev/null 2>&1 &&
  openssl x509 -req -in "$CERTS/$1.csr" -CA "$CERTS/origin-pull-ca.pem" \
    -CAkey "$CERTS/ca.key" -CAcreateserial -out "$CERTS/$1.crt" \
    -days 2 -sha256 >/dev/null 2>&1
}
gen_leaf agent.youtab.io agent.youtab.io || exit 1
gen_leaf client "cloudflare origin pull test" || exit 1
pass "server certificate and an origin-pull client certificate signed by that CA"

# The client must be WILLING to offer TLS 1.0/1.1, or the rejections below
# prove nothing about the server. Buster pins MinProtocol=TLSv1.2 and
# SECLEVEL=2 system-wide, which would do exactly that. nginx inherits this
# too, which is the point: it leaves `ssl_protocols` as the only thing in the
# system that can refuse a legacy ClientHello.
cat > /etc/ssl/openssl-legacy.cnf <<'CNF'
openssl_conf = default_conf
[ default_conf ]
ssl_conf = ssl_sect
[ ssl_sect ]
system_default = system_default_sect
[ system_default_sect ]
MinProtocol = None
CipherString = DEFAULT@SECLEVEL=0
CNF
export OPENSSL_CONF=/etc/ssl/openssl-legacy.cnf

section "install the artifact, and a legacy control mutated from it"
rm -f /etc/nginx/conf.d/default.conf
cp "$CONF_SRC" /etc/nginx/conf.d/agent.youtab.io.conf

control=/etc/nginx/conf.d/zz-legacy-control.conf
sed -e 's/^    listen 443 ssl http2;$/    listen 9443 ssl http2;/' \
    -e 's/^    listen \[::\]:443 ssl http2;$/    listen [::]:9443 ssl http2;/' \
    -e 's/^    ssl_protocols TLSv1.2 TLSv1.3;$/    ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;\n    ssl_ciphers DEFAULT@SECLEVEL=0;/' \
    "$CONF_SRC" > "$control"

# Prove the mutation landed. A sed that silently matched nothing would leave
# the control identical to the real block, both would refuse TLS 1.0, and the
# preflight below would fail for a reason that has nothing to do with whether
# legacy TLS is observable.
if grep -q '^    ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3;$' "$control" &&
   grep -q '^    listen 9443 ssl http2;$' "$control" &&
   ! grep -q '^    ssl_protocols TLSv1.2 TLSv1.3;$' "$control"; then
  pass "legacy control on :9443 carries the mutated floor TLSv1 TLSv1.1 TLSv1.2 TLSv1.3"
else
  fail "the legacy control mutation did not land; the preflight would be meaningless"
  exit 1
fi

section "nginx -t"
if nginx -t 2>&1 | sed 's/^/  /'; then
  pass "nginx -t accepts the artifact on nginx ${nginx_version}"
else
  fail "nginx -t rejected the artifact"
  exit 1
fi

section "start nginx"
nginx -g 'daemon off;' &
nginx_pid=$!
ready=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if (exec 3<>/dev/tcp/127.0.0.1/443) 2>/dev/null; then ready=1; break; fi
  sleep 0.25
done
if [ "$ready" -ne 1 ] || ! kill -0 "$nginx_pid" 2>/dev/null; then
  fail "nginx never accepted a connection on :443"
  exit 1
fi
pass "nginx is listening on :443 (artifact) and :9443 (legacy control)"

# ---------------------------------------------------------------------------
# Handshakes.
#
# Two different lines of s_client output are needed, because each one is
# unreliable in a different way and neither is enough alone:
#
#   `New, <version>, Cipher is <cipher>`
#       Trustworthy for DID THE HANDSHAKE COMPLETE -- it reads `(NONE)` when
#       it did not. Useless for WHICH PROTOCOL: that field is
#       SSL_CIPHER_get_version(), the version the CIPHER was introduced in.
#       ECDHE-RSA-AES256-SHA reports `TLSv1.0` on a TLS 1.1 session, so a
#       gate keyed on it reports TLS 1.1 as TLS 1.0 and fails a correct run.
#
#   `    Protocol  : <version>` in the SSL-Session block
#       The negotiated protocol -- but printed even for a REFUSED handshake,
#       where it echoes what the client asked for. Read on its own it reports
#       every rejection as an acceptance.
#
# So: completion comes from the first, protocol from the second, and the
# second is only consulted once the first says a handshake happened.
#
# Protocols are matched as patterns because OpenSSL spells TLS 1.0 both
# `TLSv1` and `TLSv1.0` depending on which call produced the string.
# ---------------------------------------------------------------------------
http_request() {
  printf 'GET /api/health HTTP/1.1\r\nHost: agent.youtab.io\r\nConnection: close\r\n\r\n'
}

ATTEMPT_RC=0
ATTEMPT_PROTO=""
ATTEMPT_CIPHER=""
ATTEMPT_OK=0
attempt() { # port, protocol flag, [no-cert]
  local port="$1" flag="$2" mode="${3:-with-cert}" out
  local args=(-connect "127.0.0.1:${port}" -servername agent.youtab.io
              -CAfile "$CERTS/origin-pull-ca.pem" "$flag"
              -cipher "DEFAULT@SECLEVEL=0")
  [ "$mode" = "with-cert" ] && args+=(-cert "$CERTS/client.crt" -key "$CERTS/client.key")
  # A real request rather than /dev/null. Closing immediately left TLS 1.3
  # without a session block at all -- the ticket arrives post-handshake, so
  # s_client had nothing to print and the protocol read back empty.
  out="$(http_request | openssl s_client "${args[@]}" 2>&1)"
  ATTEMPT_RC=$?
  ATTEMPT_CIPHER="$(printf '%s\n' "$out" | sed -n 's/^New, .*, Cipher is \(.*\)$/\1/p' | head -1)"
  ATTEMPT_PROTO="$(printf '%s\n' "$out" | sed -n 's/^ *Protocol *: *//p' | head -1)"
  if [ -z "$ATTEMPT_PROTO" ]; then
    # Fall back to the cipher's own version. It under-reports (a TLS 1.0-era
    # cipher on a TLS 1.2 session reads TLSv1.0), so it can only turn a real
    # pass into a failure, never a failure into a pass -- and every attempt
    # here pins min == max protocol, so the negotiated version is not in
    # doubt anyway.
    ATTEMPT_PROTO="$(printf '%s\n' "$out" | sed -n 's/^New, \(.*\), Cipher is .*$/\1/p' | head -1)"
    [ "$ATTEMPT_PROTO" = "(NONE)" ] && ATTEMPT_PROTO=""
  fi
  ATTEMPT_OK=0
  if [ "$ATTEMPT_RC" -eq 0 ] && [ -n "$ATTEMPT_CIPHER" ] && [ "$ATTEMPT_CIPHER" != "(NONE)" ]; then
    ATTEMPT_OK=1
  fi
}

evidence() {
  if [ "$ATTEMPT_OK" -eq 1 ]; then
    printf '        openssl: rc=%s handshake=completed protocol=%s cipher=%s\n' \
      "$ATTEMPT_RC" "${ATTEMPT_PROTO:-unknown}" "${ATTEMPT_CIPHER:-none}"
  else
    # Not "protocol=..." here: on a refused handshake s_client echoes the
    # version that was REQUESTED, and printing it next to the word refused
    # reads as though it had been negotiated.
    printf '        openssl: rc=%s handshake=refused (requested %s, cipher %s)\n' \
      "$ATTEMPT_RC" "${ATTEMPT_PROTO:-unknown}" "${ATTEMPT_CIPHER:-none}"
  fi
}

expect_accept() { # label, port, flag, extended regex the protocol must match
  attempt "$2" "$3"
  if [ "$ATTEMPT_OK" -eq 1 ] && printf '%s' "$ATTEMPT_PROTO" | grep -Eq "$4"; then
    pass "$1"
  else
    fail "$1 -- expected a completed handshake with a protocol matching $4"
  fi
  evidence
}

expect_reject() { # label, port, flag
  attempt "$2" "$3"
  if [ "$ATTEMPT_OK" -eq 0 ]; then
    pass "$1"
  else
    fail "$1 -- the handshake COMPLETED as ${ATTEMPT_PROTO}; the floor is not enforced"
  fi
  evidence
}

section "non-vacuity preflight: legacy TLS must be observable in this environment"
expect_accept "mutated control :9443 negotiates TLS 1.0" 9443 -tls1 '^TLSv1(\.0)?$'
expect_accept "mutated control :9443 negotiates TLS 1.1" 9443 -tls1_1 '^TLSv1\.1$'
if [ "$failures" -ne 0 ]; then
  printf '\nFAIL: legacy TLS could not be negotiated even against a server that permits it.\n'
  printf '      This environment cannot observe the exposure, so the rejections below\n'
  printf '      would pass without meaning anything. Not reporting a pass.\n'
  kill "$nginx_pid" 2>/dev/null
  exit 1
fi

section "handshake matrix against the committed artifact (:443)"
expect_accept "TLS 1.2 ACCEPTED" 443 -tls1_2 '^TLSv1\.2$'
expect_accept "TLS 1.3 ACCEPTED" 443 -tls1_3 '^TLSv1\.3$'
expect_reject "TLS 1.0 REJECTED" 443 -tls1
expect_reject "TLS 1.1 REJECTED" 443 -tls1_1

# ---------------------------------------------------------------------------
# Origin pulls and the allowlist, at the HTTP layer.
#
# `ssl_verify_client on` does NOT abort the TLS handshake when no certificate
# is presented, which is worth stating because the obvious test asserts that
# it does -- and that test fails against a correctly configured nginx. nginx
# installs a verify callback that always returns 1 and defers the decision, so
# the handshake completes and the request is refused afterwards with nginx's
# internal 496, served as `400 No required SSL certificate was sent`.
#
# So the refusal is an HTTP status, and the two controls separate cleanly:
#
#   no certificate            -> 400   rejected before the access phase
#   certificate not from the CA -> 400   same
#   valid origin-pull certificate -> 403   certificate accepted, then refused
#                                          by `deny all` for not being an edge
#
# 403 is therefore the positive control for origin pulls as well as the proof
# that the allowlist is live: reaching it means the certificate check passed.
# ---------------------------------------------------------------------------
section "authenticated origin pulls and the allowlist, at the HTTP layer"

# A certificate that is perfectly valid and simply not signed by the
# origin-pull CA. Without this, "rejects a request with no certificate" is
# consistent with an nginx that accepts any certificate at all.
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -sha256 \
  -keyout "$CERTS/rogue.key" -out "$CERTS/rogue.crt" \
  -subj "/CN=not the origin pull CA" >/dev/null 2>&1

http_status() { # mode: no-cert | client-cert | rogue-cert
  local mode="$1"
  local args=(-quiet -connect 127.0.0.1:443 -servername agent.youtab.io
              -CAfile "$CERTS/origin-pull-ca.pem" -tls1_2
              -cipher "DEFAULT@SECLEVEL=0")
  case "$mode" in
    client-cert) args+=(-cert "$CERTS/client.crt" -key "$CERTS/client.key") ;;
    rogue-cert)  args+=(-cert "$CERTS/rogue.crt" -key "$CERTS/rogue.key") ;;
  esac
  http_request | openssl s_client "${args[@]}" 2>/dev/null | head -1 | tr -d '\r'
}

status_none="$(http_status no-cert)"
case "$status_none" in
  *400*) pass "no client certificate -- refused ${status_none}" ;;
  *) fail "no client certificate should be refused 400, got '${status_none}'" ;;
esac

status_rogue="$(http_status rogue-cert)"
case "$status_rogue" in
  *400*) pass "certificate not signed by the origin-pull CA -- refused ${status_rogue}" ;;
  *) fail "an untrusted client certificate should be refused 400, got '${status_rogue}'" ;;
esac

status_valid="$(http_status client-cert)"
case "$status_valid" in
  *403*)
    pass "valid origin-pull certificate accepted, then refused 403 by the allowlist"
    pass "  -- 403 rather than 400 is the positive control for ssl_verify_client"
    pass "  -- 403 rather than 502 is the proof that deny all is live"
    ;;
  *400*) fail "a CA-signed client certificate was refused 400; origin pulls are rejecting a valid cert" ;;
  *) fail "expected 403 from the allowlist, got '${status_valid}'" ;;
esac

kill "$nginx_pid" 2>/dev/null
wait "$nginx_pid" 2>/dev/null

printf '\n===== origin TLS gate =====\n'
if [ "$failures" -ne 0 ]; then
  printf 'FAIL: %d check(s) failed\n' "$failures"
  exit 1
fi
printf 'PASS: nginx %s serves TLSv1.2/TLSv1.3 only, refuses TLS 1.0/1.1 that this\n' "$nginx_version"
printf '      same client negotiated against the mutated control, requires an\n'
printf '      origin-pull client certificate, and enforces the allowlist.\n'
