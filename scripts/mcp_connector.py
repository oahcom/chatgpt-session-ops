#!/usr/bin/env python3
"""MCP Connector Eligibility & Management CLI.

Two capabilities, both machine-verifiable:

  inspect      Check an arbitrary MCP endpoint: transport, OAuth discovery,
               initialize, tools/list. Refuses unsafe URLs (SSRF guard).

  eligibility  Determine whether a ChatGPT account may create a custom MCP
               connector, from its plan and subscription state.

Design note: connector creation is intentionally not implemented here.
Registration of an EXISTING connector (OAuth connect per account) is supported
via the browser/Bearer flow; plan tier does not block connecting a published
connector. This tool inspects endpoints and reports eligibility facts only.

Usage:
    python mcp_connector.py inspect --url https://example.com/mcp [--json]
    python mcp_connector.py eligibility --account acct1 [--json]
    python mcp_connector.py eligibility --accounts acct1,acct2,acct10

Exit codes:
    0  every requested check completed (including a clean "denied" verdict)
    1  one or more checks failed for an operational reason; this includes a
       missing or empty credential file, reported as AUTH_MISSING
    2  bad arguments only -- produced by argparse, not by main()
    4  eligibility could not be determined

    AUTH_MISSING exits 1 on purpose, not 2. The 2 above covers argparse usage
    errors alone (argparse calls sys.exit(2) itself, before main() returns).
    Do not renumber AUTH_MISSING to 2: callers key on the exit code, so that
    would silently change existing behaviour.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_DIR = os.path.join(SKILL_DIR, 'accounts')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36')

# Plans that may create a custom MCP connector. Pro is intentionally separate:
# it gets read/fetch only, which is not full connector creation.
FULL_MCP_PLANS = {'business', 'enterprise', 'edu'}
LIMITED_MCP_PLANS = {'pro'}

# RFC 6598 shared address space. is_private is False for these and is_global
# has moved between CPython releases, so pin the range explicitly: it contains
# cloud instance-metadata endpoints (100.100.100.200 on Alibaba Cloud).
CGNAT_NETWORK = ipaddress.ip_network('100.64.0.0/10')


# ---------------------------------------------------------------- errors

class CheckError(Exception):
    def __init__(self, code: str, detail: str = ''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- url guard

def assert_safe_url(url: str) -> None:
    """Reject anything that could turn this CLI into an SSRF primitive."""
    p = urlparse(url)
    if p.scheme != 'https':
        raise CheckError('INVALID_URL', 'scheme must be https')
    if p.username or p.password:
        raise CheckError('INVALID_URL', 'credentials in URL are not allowed')
    if p.fragment:
        raise CheckError('INVALID_URL', 'fragment is not allowed')
    if not p.hostname:
        raise CheckError('INVALID_URL', 'missing host')
    try:
        port = p.port or 443
    except ValueError as e:
        raise CheckError('INVALID_URL', f'malformed URL: {url!r} has an invalid port') from e
    try:
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise CheckError('UNSAFE_ENDPOINT', f'dns resolution failed: {e}') from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip in CGNAT_NETWORK or not ip.is_global or ip.is_private
                or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise CheckError('UNSAFE_ENDPOINT', f'{p.hostname} resolves to {ip}')


# ---------------------------------------------------------------- http

def _request(url: str, method: str = 'GET', body: bytes | None = None,
             headers: dict | None = None, timeout: int = 20) -> tuple[int, dict, str]:
    h = {'User-Agent': UA, 'Accept': 'application/json'}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8', 'replace')
    except Exception as e:  # noqa: BLE001 - transport faults must not abort the report
        return 0, {}, '<transport error: %s>' % str(e)[:120]


def _parse_mcp_body(raw: str) -> dict | None:
    """MCP streamable HTTP may answer as SSE; take the last data frame."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith('{'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    payload = None
    for line in raw.splitlines():
        if line.startswith('data:'):
            chunk = line[5:].strip()
            if chunk:
                payload = chunk
    if payload:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------- inspect

@dataclass
class InspectResult:
    url: str
    reachable: bool = False
    status: int = 0
    auth_required: bool = False
    resource_metadata: str = ''
    authorization_server: str = ''
    pkce: bool = False
    dcr: bool = False
    initialize: str = 'NOT_RUN'
    tools_list: str = 'NOT_RUN'
    tool_count: int = 0
    server: str = ''
    error: str = ''
    notes: list[str] = field(default_factory=list)


def inspect(url: str) -> InspectResult:
    res = InspectResult(url=url)
    try:
        assert_safe_url(url)
    except CheckError as e:
        res.error = e.code
        res.notes.append(e.detail)
        return res

    # 1. probe the endpoint itself
    try:
        status, headers, _ = _request(url, 'POST',
                                      body=json.dumps({
                                          'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                          'params': {'protocolVersion': '2024-11-05',
                                                     'capabilities': {},
                                                     'clientInfo': {'name': 'mcp-connector-cli',
                                                                    'version': '1.0'}},
                                      }).encode(),
                                      headers={'Content-Type': 'application/json',
                                               'Accept': 'application/json, text/event-stream'})
        res.status = status
        res.reachable = 0 < status < 500
        if status == 0:
            res.error = 'MCP_UNREACHABLE'
            res.notes.append('transport failure before any HTTP status')
    except Exception as e:  # noqa: BLE001 - report as unreachable
        res.error = 'MCP_UNREACHABLE'
        res.notes.append(str(e)[:160])
        return res

    if status == 401:
        res.auth_required = True
        wa = headers.get('WWW-Authenticate', '') or headers.get('www-authenticate', '')
        for part in wa.split(','):
            part = part.strip()
            if part.startswith('resource_metadata='):
                res.resource_metadata = part.split('=', 1)[1].strip('"')
        if not res.resource_metadata:
            res.notes.append('401 without resource_metadata; OAuth discovery unavailable')

    # 2. protected resource metadata
    if res.resource_metadata:
        try:
            st, _, body = _request(res.resource_metadata)
            if st == 200:
                meta = json.loads(body)
                servers = meta.get('authorization_servers') or []
                if servers:
                    res.authorization_server = servers[0]
        except Exception as e:  # noqa: BLE001
            res.notes.append(f'resource metadata: {str(e)[:100]}')

    if not res.authorization_server:
        p = urlparse(url)
        guess = f'{p.scheme}://{p.netloc}/.well-known/oauth-authorization-server'
        st, _, body = _request(guess)
        if st == 200:
            try:
                res.authorization_server = json.loads(body).get('issuer', '')
            except json.JSONDecodeError:
                pass

    # 3. authorization server metadata
    if res.authorization_server:
        st, _, body = _request(res.authorization_server.rstrip('/')
                               + '/.well-known/openid-configuration')
        if st != 200:
            st, _, body = _request(res.authorization_server.rstrip('/')
                                   + '/.well-known/oauth-authorization-server')
        if st == 200:
            try:
                asm = json.loads(body)
                methods = asm.get('code_challenge_methods_supported') or []
                res.pkce = 'S256' in methods
                res.dcr = bool(asm.get('registration_endpoint'))
            except json.JSONDecodeError:
                pass

    # 4. anonymous protocol handshake (only meaningful when no auth required)
    if not res.auth_required and status == 200:
        payload = _parse_mcp_body(_request(
            url, 'POST',
            body=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                             'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                                        'clientInfo': {'name': 'mcp-connector-cli',
                                                       'version': '1.0'}}}).encode(),
            headers={'Content-Type': 'application/json',
                     'Accept': 'application/json, text/event-stream'})[2])
        if payload and 'result' in payload:
            res.initialize = 'PASS'
            res.server = (payload['result'].get('serverInfo') or {}).get('name', '')
            st2, _, body2 = _request(
                url, 'POST',
                body=json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list',
                                 'params': {}}).encode(),
                headers={'Content-Type': 'application/json',
                         'Accept': 'application/json, text/event-stream'})
            tl = _parse_mcp_body(body2)
            if st2 == 200 and tl and 'result' in tl:
                res.tools_list = 'PASS'
                res.tool_count = len(tl['result'].get('tools') or [])
            else:
                res.tools_list = 'FAIL'
        else:
            res.initialize = 'FAIL'
    else:
        if res.auth_required:
            res.notes.append('protocol handshake skipped: endpoint requires OAuth')
        elif status != 200:
            res.notes.append('protocol handshake skipped: endpoint answered %d, '
                             'not an MCP streamable HTTP response' % status)

    return res


# ---------------------------------------------------------------- accounts

def token_path(account: str) -> str:
    if not re.fullmatch(r'acct[1-9][0-9]*', account):
        raise CheckError('INVALID_ARGUMENT', 'account must match acctN')
    if account == 'acct1':
        canonical = os.path.join(ACCOUNTS_DIR, 'acct1')
        if os.path.isdir(canonical):
            return os.path.join(canonical, 'access_token.txt')
        return os.path.join(SKILL_DIR, 'access_token.txt')
    return os.path.join(ACCOUNTS_DIR, account, 'access_token.txt')


def discover_accounts() -> list[str]:
    """Discover configured acctN directories in natural numeric order."""
    names = []
    acct1_dir = os.path.join(ACCOUNTS_DIR, 'acct1')
    legacy_acct1_token = os.path.join(SKILL_DIR, 'access_token.txt')
    if os.path.isdir(acct1_dir) or os.path.isfile(legacy_acct1_token):
        names.append('acct1')
    try:
        with os.scandir(ACCOUNTS_DIR) as entries:
            for entry in entries:
                if (entry.name != 'acct1'
                        and re.fullmatch(r'acct[1-9][0-9]*', entry.name)
                        and entry.is_dir(follow_symlinks=False)):
                    names.append(entry.name)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CheckError('ACCOUNT_DISCOVERY_FAILED',
                         f'cannot enumerate account directory: {ACCOUNTS_DIR}') from exc
    return sorted(set(names), key=lambda name: int(name[4:]))


def load_token(account: str) -> str:
    path = token_path(account)
    if not os.path.exists(path):
        raise CheckError('AUTH_MISSING', f'{account}: no access_token.txt at {path}')
    tok = open(path, encoding='utf-8').read().strip()
    if not tok:
        raise CheckError('AUTH_MISSING', f'{account}: empty token file')
    return tok


def _api(token: str, path: str) -> tuple[int, str]:
    st, _, body = _request('https://chatgpt.com' + path,
                           headers={'Authorization': 'Bearer ' + token})
    return st, body


@dataclass
class Eligibility:
    account: str
    user_id: str = ''
    email: str = ''
    plan: str = ''
    subscription_plan: str = ''
    has_active_subscription: bool = False
    capability: str = 'CUSTOM_MCP_CONNECTOR'
    eligible: bool = False
    status: str = ''
    reason: str = ''
    error: str = ''


def eligibility(account: str, capability: str = 'CUSTOM_MCP_FULL') -> Eligibility:
    out = Eligibility(account=account, capability=capability)
    try:
        tok = load_token(account)
    except CheckError as e:
        out.status = e.code
        out.reason = e.detail
        return out

    st, body = _api(tok, '/backend-api/me')
    if st == 401:
        out.status = 'AUTH_EXPIRED'
        out.reason = 'access token rejected'
        return out
    if st != 200:
        out.status = 'ELIGIBILITY_UNKNOWN'
        out.reason = f'/me returned {st}'
        return out
    try:
        me = json.loads(body)
        out.user_id = me.get('id', '')
        out.email = me.get('email', '') or ''
    except json.JSONDecodeError:
        out.status = 'ELIGIBILITY_UNKNOWN'
        out.reason = '/me was not JSON'
        return out

    st, body = _api(tok, '/backend-api/accounts/check/v4-2023-04-27')
    if st == 401:
        out.status = 'AUTH_EXPIRED'
        out.reason = 'access token rejected by accounts/check'
        return out
    if st != 200:
        out.status = 'ELIGIBILITY_UNKNOWN'
        out.reason = f'accounts/check returned {st}'
        return out
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        out.status = 'ELIGIBILITY_UNKNOWN'
        out.reason = 'accounts/check was not JSON'
        return out

    best = None
    for _, entry in (data.get('accounts') or {}).items():
        acc = (entry or {}).get('account') or {}
        ent = (entry or {}).get('entitlement') or {}
        if acc.get('is_deactivated'):
            continue
        if ent.get('subscription_plan'):
            best = (acc, ent)
            break
        if best is None:
            best = (acc, ent)

    if not best:
        out.status = 'ELIGIBILITY_UNKNOWN'
        out.reason = 'no account entry in accounts/check'
        return out

    acc, ent = best
    out.plan = (acc.get('plan_type') or '').lower()
    out.subscription_plan = ent.get('subscription_plan') or ''
    out.has_active_subscription = bool(ent.get('has_active_subscription'))

    if out.plan in FULL_MCP_PLANS:
        out.eligible = True
        out.status = 'ELIGIBLE'
        out.reason = 'plan supports full custom MCP connectors'
    elif out.plan in LIMITED_MCP_PLANS:
        out.eligible = capability == 'CUSTOM_MCP_READ_FETCH'
        out.status = 'LIMITED' if out.eligible else 'PERMISSION_DENIED'
        out.reason = ('pro grants read/fetch only, not full connector creation'
                      if not out.eligible else 'pro read/fetch capability')
    else:
        out.status = 'PERMISSION_DENIED'
        out.reason = 'PLAN_NOT_ELIGIBLE'
    return out


# ---------------------------------------------------------------- cli

def _print_inspect(r: InspectResult) -> None:
    print('MCP INSPECT')
    print('=' * 60)
    print('Endpoint      :', r.url)
    print('Reachable     :', r.reachable, f'(status {r.status})' if r.status else '')
    print('Auth required :', r.auth_required)
    if r.resource_metadata:
        print('Resource meta :', r.resource_metadata)
    if r.authorization_server:
        print('Auth server   :', r.authorization_server)
    print('PKCE S256     :', r.pkce)
    print('DCR           :', r.dcr)
    print('initialize    :', r.initialize)
    print('tools/list    :', r.tools_list, f'({r.tool_count} tools)' if r.tool_count else '')
    if r.server:
        print('server name   :', r.server)
    if r.error:
        print('ERROR         :', r.error)
    for n in r.notes:
        print('note          :', n)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='MCP connector eligibility and inspection')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_ins = sub.add_parser('inspect', help='check an arbitrary MCP endpoint')
    p_ins.add_argument('--url', required=True)
    p_ins.add_argument('--json', action='store_true')

    p_el = sub.add_parser('eligibility', help='check account plan capability')
    g = p_el.add_mutually_exclusive_group(required=True)
    g.add_argument('--account')
    g.add_argument('--accounts', help='comma separated, or "all"')
    p_el.add_argument('--capability', default='CUSTOM_MCP_FULL',
                      choices=['CUSTOM_MCP_FULL', 'CUSTOM_MCP_READ_FETCH'])
    p_el.add_argument('--json', action='store_true')

    args = ap.parse_args(argv)

    if args.cmd == 'inspect':
        r = inspect(args.url)
        if args.json:
            print(json.dumps(asdict(r), indent=2, ensure_ascii=False))
        else:
            _print_inspect(r)
        return 0 if not r.error else 1

    # eligibility
    names = discover_accounts() if args.accounts == 'all' \
        else (args.accounts or args.account).split(',')
    names = [n.strip() for n in names if n.strip()]
    if not names:
        print('No configured acctN accounts found')
        return 1
    rows = [eligibility(n, args.capability) for n in names]

    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False))
    else:
        print('MCP CONNECTOR ELIGIBILITY REPORT')
        print('=' * 60)
        for r in rows:
            print('%-6s %-16s plan=%-8s %s' %
                  (r.account, r.status, r.plan or '?', r.reason))
        print()
        print('Accounts checked  :', len(rows))
        print('Eligible          :', sum(1 for r in rows if r.eligible))
        print('Permission denied :', sum(1 for r in rows if r.status == 'PERMISSION_DENIED'))
        print('Auth errors       :', sum(1 for r in rows if r.status.startswith('AUTH')))
        print('Unknown           :', sum(1 for r in rows if r.status == 'ELIGIBILITY_UNKNOWN'))

    if any(r.status in ('AUTH_EXPIRED', 'AUTH_MISSING', 'ELIGIBILITY_UNKNOWN') for r in rows):
        return 1 if any(r.status.startswith('AUTH') for r in rows) else 4
    return 0


# ---------------------------------------------------------------- self check

assert ipaddress.ip_address('10.0.0.1').is_private
assert not ipaddress.ip_address('1.1.1.1').is_private
assert _parse_mcp_body('data: {"result":{"ok":1}}')['result']['ok'] == 1
assert _parse_mcp_body('{"result":{"ok":2}}')['result']['ok'] == 2
assert _parse_mcp_body('') is None
for bad in ('http://x/mcp', 'https://user:pw@x/mcp', 'https://x/mcp#frag',
            'https://100.100.100.200/mcp'):
    try:
        assert_safe_url(bad)
        raise SystemExit('guard failed to reject ' + bad)
    except CheckError:
        pass


if __name__ == '__main__':
    sys.exit(main())
