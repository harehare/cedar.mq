#!/usr/bin/env python3
"""Regenerates cedar_conformance_tests.mq from the output of the Cedar CLI.

Requires `cedar` (cargo install cedar-policy-cli) and `mq` on PATH. Run from anywhere:

    python3 scripts/gen_conformance_tests.py

Each expression case is evaluated by `cedar evaluate` and each authorization trial by `cedar authorize`;
the results become the expected values of the generated mq tests. The cases are fixed (seeded) so the output
is reproducible for a given Cedar CLI version. Integers beyond 2^53 are left out (see README Limitations).
"""
import itertools
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- fixtures and helpers


ENTITIES = [
    {"uid": {"type": "User", "id": "alice"},
     "attrs": {"name": "Alice", "age": 30, "admin": True, "tags": ["a", "b"], "nums": [1, 2, 3],
               "manager": {"__entity": {"type": "User", "id": "bob"}},
               "profile": {"city": "Tokyo", "geo": {"lat": 35, "lon": 139}},
               "addr": {"__extn": {"fn": "ip", "arg": "10.1.2.3"}},
               "score": {"__extn": {"fn": "decimal", "arg": "2.50"}},
               "since": {"__extn": {"fn": "datetime", "arg": "2024-10-15T11:35:00Z"}},
               "ttl": {"__extn": {"fn": "duration", "arg": "1h30m"}}},
     "parents": [{"type": "Group", "id": "staff"}, {"type": "Group", "id": "dev"}],
     "tags": {"role": "admin", "team": "core", "level": 3}},
    {"uid": {"type": "User", "id": "bob"}, "attrs": {"name": "Bob", "age": 17, "admin": False},
     "parents": [{"type": "Group", "id": "staff"}]},
    {"uid": {"type": "Group", "id": "staff"}, "attrs": {}, "parents": [{"type": "Group", "id": "all"}]},
    {"uid": {"type": "Group", "id": "dev"}, "attrs": {}, "parents": [{"type": "Group", "id": "all"}]},
    {"uid": {"type": "Group", "id": "all"}, "attrs": {}, "parents": []},
    {"uid": {"type": "Photo", "id": "p1"},
     "attrs": {"public": True, "owner": {"__entity": {"type": "User", "id": "alice"}}, "size": 1024},
     "parents": [{"type": "Album", "id": "trip"}]},
    {"uid": {"type": "Photo", "id": "p2"}, "attrs": {"public": False}, "parents": [{"type": "Album", "id": "work"}]},
    {"uid": {"type": "Album", "id": "trip"}, "attrs": {}, "parents": [{"type": "Album", "id": "all"}]},
    {"uid": {"type": "Album", "id": "work"}, "attrs": {}, "parents": []},
    {"uid": {"type": "Album", "id": "all"}, "attrs": {}, "parents": []},
    {"uid": {"type": "Action", "id": "view"}, "attrs": {}, "parents": [{"type": "Action", "id": "read"}]},
    {"uid": {"type": "Action", "id": "read"}, "attrs": {}, "parents": []},
    {"uid": {"type": "Action", "id": "edit"}, "attrs": {}, "parents": []},
]

CONTEXT = {
    "mfa": True, "n": 3, "neg": -3, "name": "report.pdf", "empty": "", "tags": ["x", "y"], "none": [],
    "nested": {"a": {"b": 1}}, "unicode": "日本語",
    "ip": {"__extn": {"fn": "ip", "arg": "10.0.0.5"}},
    "dec": {"__extn": {"fn": "decimal", "arg": "1.5"}},
    "at": {"__extn": {"fn": "datetime", "arg": "2024-10-15T11:35:00.123Z"}},
    "dur": {"__extn": {"fn": "duration", "arg": "2h"}},
    "who": {"__entity": {"type": "User", "id": "bob"}},
}

REQUEST_CEDAR = {"principal": 'User::"alice"', "action": 'Action::"view"', "resource": 'Photo::"p1"', "context": CONTEXT}


def sh(args, timeout=60):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def setup(workdir, request=None, entities=None):
    write(os.path.join(workdir, "e.json"), ENTITIES if entities is None else entities)
    write(os.path.join(workdir, "r.json"), REQUEST_CEDAR if request is None else request)


# ---------------------------------------------------------------- cedar CLI

def cedar_eval_one(workdir, expr):
    rc, out, err = sh(["cedar", "evaluate", "--request-json", os.path.join(workdir, "r.json"),
                       "--entities", os.path.join(workdir, "e.json"), "--", expr])
    text = (out + err).strip()
    if rc != 0 or "failed to" in text or "×" in text:
        return {"err": text}
    return {"ok": text}


def esc(s):
    """Escapes s for use inside a Cedar string literal."""
    table = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0"}
    return "".join(table.get(ch, ch) for ch in s)


# ---------------------------------------------------------------- cases
suites = {}

# 1. operator x operand type matrix (type-error parity)
POOL = ['1', '-1', '0', '"a"', '""', 'true', 'false', '[1]', '[]', '[1, "a"]', '{a: 1}', '{}',
        'User::"alice"', 'Group::"staff"', 'ip("1.1.1.1")', 'ip("10.0.0.0/8")', 'decimal("1.0")',
        'datetime("2024-01-01")', 'duration("1h")', 'principal.name', 'principal.profile', 'context.ip',
        '[User::"alice"]', '[Group::"staff", Group::"all"]']
BIN = ['+', '-', '*', '<', '<=', '>', '>=', '==', '!=', '&&', '||', 'in']
cases = []
for a, b in itertools.product(POOL, POOL):
    for op in BIN:
        cases.append("%s %s %s" % (a, op, b))
suites["binary-matrix"] = cases

cases = []
for a in POOL:
    cases += ["!%s" % a, "-%s" % a, "%s.isEmpty()" % a, "if %s then 1 else 2" % a,
              "%s is User" % a, "%s is Group in Group::\"all\"" % a, "%s has name" % a, "%s has \"a\"" % a,
              "%s like \"a*\"" % a, "%s.name" % a, "%s.a" % a, "%s.isIpv4()" % a, "%s.isInRange(ip(\"1.0.0.0/8\"))" % a,
              "%s.lessThan(decimal(\"2.0\"))" % a, "%s.toDate()" % a, "%s.toTime()" % a, "%s.toMinutes()" % a,
              "%s.toMilliseconds()" % a, "%s.offset(duration(\"1h\"))" % a, "%s.durationSince(datetime(\"2024-01-01\"))" % a,
              "ip(%s)" % a, "decimal(%s)" % a, "datetime(%s)" % a, "duration(%s)" % a]
    for b in POOL:
        cases += ["%s.contains(%s)" % (a, b), "%s.containsAll(%s)" % (a, b), "%s.containsAny(%s)" % (a, b),
                  "%s.hasTag(%s)" % (a, b), "%s.getTag(%s)" % (a, b), "%s.isInRange(%s)" % (a, b),
                  "%s.lessThan(%s)" % (a, b), "%s.greaterThanOrEqual(%s)" % (a, b),
                  "%s.offset(%s)" % (a, b), "%s.durationSince(%s)" % (a, b), "%s is User in %s" % (a, b),
                  "if %s then %s else %s" % (a, b, b)]
suites["unary-and-methods"] = cases

# 2. constructor strings
IP = ['', '1.2.3.4', '0.0.0.0', '255.255.255.255', '256.1.1.1', '1.2.3', '1.2.3.4.5', '01.2.3.4', '1.2.3.04', '+1.2.3.4',
      ' 1.2.3.4', '1.2.3.4 ', '1.2.3.4/0', '1.2.3.4/32', '1.2.3.4/33', '1.2.3.4/', '1.2.3.4/-1', '1.2.3.4/+8', '1.2.3.4/08',
      '1.2.3.4/8/8', '/8', '::', '::1', '::1/128', '::1/129', '1::', '1::2', '1:2:3:4:5:6:7:8', '1:2:3:4:5:6:7::', '::2:3:4:5:6:7:8',
      '1:2:3:4:5:6:7:8:9', '1:2:3:4:5:6:7', '1::2::3', ':::', ':1:2:3:4:5:6:7', '1:2:3:4:5:6:7:', 'ffff:FFFF:abcd:ABCD::',
      'g::1', '12345::', '::ffff:1.2.3.4', '::1.2.3.4', '1:2:3:4:5:6:1.2.3.4', '1:2:3:4:5:6:7:1.2.3.4', '::ffff:1.2.3', '::ffff:256.1.1.1',
      '1.2.3.4::', 'fe80::1%eth0', 'ff00::/8', '2001:db8::/32', '::/0', '0:0:0:0:0:0:0:0', '1:2:3:4:5:6:7:8/64', 'abc', '1.2.3.-4',
      '1:2:3:4:5:6::7', '1:2:3:4:5:6:7::8', '::0:0:0:0:0:0:0', '1:0:0:0:0:0:0:0:', '[::1]']
DEC = ['', '0', '0.0', '1.0', '1.5', '-1.5', '1.12345', '1.1234', '.5', '5.', '+1.0', '1.0e3', '1e3', '00.1', '000.0001', '-0.0', '-.5',
       '922337203685477.5807', '922337203685477.5808', '-922337203685477.5808', '-922337203685477.5809', '922337203685478.0',
       '1,5', '1.5 ', ' 1.5', '1.-5', '1..5', '١.٥', '9999999999999999999999.0', '-0.0001', '0.0001', '12345.1234', 'abc', '--1.0']
DT = ['', '2024-10-15', '2024-10-15T', '2024-10-15T00:00:00Z', '2024-10-15T00:00:00', '2024-10-15T00:00Z', '2024-10-15 00:00:00Z',
      '2024-10-15T00:00:00.1Z', '2024-10-15T00:00:00.12Z', '2024-10-15T00:00:00.123Z', '2024-10-15T00:00:00.1234Z', '2024-10-15T00:00:00.000+0100',
      '2024-10-15T00:00:00+01:00', '2024-10-15T00:00:00+01', '2024-10-15T00:00:00+2359', '2024-10-15T00:00:00-2359', '2024-10-15T00:00:00+2400',
      '2024-10-15T00:00:00+0060', '2024-10-15T00:00:00+0059', '2024-10-15T24:00:00Z', '2024-10-15T23:59:59Z', '2024-10-15T23:59:60Z',
      '2024-10-15T23:60:00Z', '2024-02-29', '2023-02-29', '1900-02-29', '2000-02-29', '2100-02-29', '2024-02-30', '2024-04-31', '2024-04-30',
      '2024-00-01', '2024-13-01', '2024-01-00', '2024-01-32', '0000-01-01', '0000-01-01T00:00:00+2359', '0000-01-01T00:00:00-2359',
      '9999-12-31', '9999-12-31T23:59:59-2359', '9999-12-31T23:59:59+2359', '9999-12-31T23:59:59Z', '10000-01-01', '999-01-01', '24-10-15',
      '2024-1-5', '2024/10/15', '20241015', '2024-10-15t00:00:00z', '2024-10-15T00:00:00z', '1969-12-31', '1970-01-01', '1970-01-01T00:00:00Z',
      '1969-12-31T23:59:59.999Z', '2024-10-15T00:00:00.000Z', ' 2024-10-15', '2024-10-15 ', '-2024-10-15', '+2024-10-15', 'abc']
DUR = ['', '-', '1', '1d', '1h', '1m', '1s', '1ms', '0d', '0ms', '-0ms', '1d2h3m4s5ms', '5ms', '-5ms', '--5ms', '+5ms', '1h1h', '1h1d', '1s1m',
       '1m1ms', '1ms1s', '1d1', '1 d', '1D', '1.5h', '0.5s', '24h', '25h', '90m', '3600s', '99999999999999999999ms', '9223372036854775807ms',
       '9223372036854775808ms', '-9223372036854775808ms', '-9223372036854775809ms', '106751991167d', '106751991168d', '1d-1h', 'd', 'ms', '1x',
       '001h', '1h ', ' 1h', '1 h', '-1d12h', '-1d-12h', '1h30m', '1d23h59m59s999ms']
cases = []
for fn, vals in [('ip', IP), ('decimal', DEC), ('datetime', DT), ('duration', DUR)]:
    for s in vals:
        lit = '"' + esc(s) + '"'
        cases.append('%s(%s)' % (fn, lit))
suites["constructors"] = cases

# constructor equality / ordering across equal-valued spellings
cases = []
for a, b in itertools.product(['ip("1.2.3.4")', 'ip("1.2.3.4/32")', 'ip("1.2.3.4/24")', 'ip("1.2.3.0/24")', 'ip("::1")', 'ip("0:0:0:0:0:0:0:1")',
                               'ip("::ffff:1.2.3.4")', 'ip("::ffff:102:304")', 'ip("::1/128")', 'ip("::/0")', 'ip("0.0.0.0/0")'], repeat=2):
    cases += ['%s == %s' % (a, b), '%s.isInRange(%s)' % (a, b)]
for a, b in itertools.product(['decimal("1.0")', 'decimal("1.00")', 'decimal("01.0")', 'decimal("-0.0")', 'decimal("0.0")', 'decimal("-0.0001")',
                               'decimal("0.0001")', 'decimal("1.5")', 'decimal("-1.5")', 'decimal("-1.4999")'], repeat=2):
    cases += ['%s == %s' % (a, b)] + ['%s.%s(%s)' % (a, m, b) for m in ('lessThan', 'lessThanOrEqual', 'greaterThan', 'greaterThanOrEqual')]
for a, b in itertools.product(['datetime("2024-10-15")', 'datetime("2024-10-15T00:00:00Z")', 'datetime("2024-10-15T01:00:00+0100")',
                               'datetime("2024-10-14T23:00:00-0100")', 'datetime("1969-12-31")', 'datetime("1970-01-01")',
                               'datetime("2024-10-15T00:00:00.001Z")'], repeat=2):
    cases += ['%s == %s' % (a, b), '%s != %s' % (a, b), '%s < %s' % (a, b), '%s <= %s' % (a, b), '%s > %s' % (a, b), '%s >= %s' % (a, b),
              '%s.durationSince(%s)' % (a, b)]
for a, b in itertools.product(['duration("1d")', 'duration("24h")', 'duration("-1d")', 'duration("0ms")', 'duration("1ms")',
                               'duration("-1ms")', 'duration("1h30m")', 'duration("90m")'], repeat=2):
    cases += ['%s == %s' % (a, b), '%s < %s' % (a, b), '%s <= %s' % (a, b), '%s > %s' % (a, b), '%s >= %s' % (a, b)]
suites["extension-compare"] = cases

# 3. ip predicates / ranges
cases = []
for s in ['127.0.0.1', '127.255.255.255', '128.0.0.1', '126.255.255.255', '127.0.0.1/8', '127.0.0.0/7', '127.1.0.0/16', '224.0.0.0', '239.255.255.255',
          '240.0.0.0', '223.255.255.255', '224.0.0.0/4', '224.0.0.0/3', '::1', '::', '::2', '::1/128', '::1/127', 'ff00::', 'ff02::1', 'fe00::',
          'ff00::/8', 'ff00::/7', '0.0.0.0', '10.0.0.0/8', '::ffff:127.0.0.1', '::ffff:224.0.0.1']:
    for m in ('isIpv4', 'isIpv6', 'isLoopback', 'isMulticast'):
        cases.append('ip("%s").%s()' % (s, m))
for a, b in itertools.product(['192.168.0.1', '192.168.0.1/24', '192.168.0.0/24', '192.168.0.255/24', '192.168.0.1/28', '192.168.1.1', '10.0.0.0/8',
                               '0.0.0.0/0', '255.255.255.255', '255.255.255.255/32', '2001:db8::1', '2001:db8::/32', '2001:db8:1::/48',
                               '::/0', '::1', '::ffff:192.168.0.1'], repeat=2):
    cases.append('ip("%s").isInRange(ip("%s"))' % (a, b))
suites["ip-ranges"] = cases

# 4. datetime / duration methods
cases = []
DTS = ['2024-10-15T11:35:00.123Z', '2024-10-15', '1970-01-01', '1969-12-31T23:59:59.999Z', '1969-12-31T00:00:00Z', '1960-06-15T12:00:00Z',
       '0000-01-01', '0000-03-01', '9999-12-31T23:59:59.999Z', '2024-02-29T23:59:59Z', '2023-12-31T23:59:59Z', '1900-03-01', '2000-02-29T12:00:00+0530',
       '1969-12-31T23:00:00-0100', '2024-10-15T11:35:00-2359']
DURS = ['1ms', '-1ms', '1s', '-1s', '1m', '1h', '-1h', '1d', '-1d', '36500d', '-36500d', '1d12h', '0ms', '999ms', '-999ms', '59s999ms',
        '3650000d', '-3650000d', '106751991167d']
for d in DTS:
    cases += ['datetime("%s").toDate()' % d, 'datetime("%s").toTime()' % d]
    for u in DURS:
        cases.append('datetime("%s").offset(duration("%s"))' % (d, u))
    for e in DTS[:6]:
        cases.append('datetime("%s").durationSince(datetime("%s"))' % (d, e))
for u in DURS + ['-90s', '90s', '-90m', '-1d12h', '86399999ms', '-86399999ms', '-3599999ms', '3599999ms', '-59999ms', '59999ms', '9223372036854775807ms',
                 '-9223372036854775807ms']:
    for m in ('toMilliseconds', 'toSeconds', 'toMinutes', 'toHours', 'toDays'):
        cases.append('duration("%s").%s()' % (u, m))
suites["datetime-duration"] = cases

# 5. hierarchy
cases = []
ENTS = ['User::"alice"', 'User::"bob"', 'User::"ghost"', 'Group::"staff"', 'Group::"dev"', 'Group::"all"', 'Photo::"p1"', 'Photo::"p2"', 'Album::"trip"',
        'Album::"work"', 'Album::"all"', 'Action::"view"', 'Action::"read"', 'Action::"edit"', 'Other::"x"', 'principal', 'resource', 'action', 'context.who']
for a, b in itertools.product(ENTS, ENTS):
    cases += ['%s in %s' % (a, b), '%s == %s' % (a, b)]
for a in ENTS:
    cases += ['%s in [%s, %s]' % (a, 'Group::"staff"', 'Album::"work"'), '%s in []' % a, '%s in [Group::"all", 1]' % a, '%s in [Group::"zz", 1]' % a,
              '%s in [[Group::"all"]]' % a, '%s is User' % a, '%s is Group in Group::"all"' % a, '%s is Photo in Album::"all"' % a,
              '%s is User in User::"alice"' % a, '%s is Foo::Bar' % a]
suites["hierarchy"] = cases

# 6. attributes / has / tags
cases = []
PATHS = ['name', 'age', 'admin', 'tags', 'nums', 'manager', 'profile', 'profile.city', 'profile.geo', 'profile.geo.lat', 'profile.nope', 'nope', 'nope.x',
         'addr', 'score', 'since', 'ttl', 'name.x', 'manager.name', 'manager.nope', 'size', 'public', 'owner', 'owner.name']
for e in ['principal', 'resource', 'User::"alice"', 'User::"bob"', 'User::"ghost"', 'Group::"staff"', 'Photo::"p2"', 'context', 'context.nested',
          'context.who', 'context.nested.a', '{a: {b: 1}}', '{}', '{"a b": 1}']:
    for p in PATHS:
        cases.append('%s has %s' % (e, p))
        acc = ''.join('.' + seg for seg in p.split('.'))
        cases.append('%s%s' % (e, acc))
    for t in ['"role"', '"team"', '"level"', '"nope"', '""', '1']:
        cases += ['%s.hasTag(%s)' % (e, t), '%s.getTag(%s)' % (e, t)]
    cases += ['%s has "a b"' % e, '%s["a b"]' % e, '%s["name"]' % e, '%s has "name"' % e]
suites["attributes"] = cases

# 7. like
cases = []
PATS = ['', '*', '**', 'a', 'a*', '*a', '*a*', 'a*b', 'a*b*c', '*a*b*', 'ab*ab', 'a**b', '\\*', 'a\\*b', '*\\*', '\\**', 'x*y*z', 'report*', '*.pdf', 'r*t.p*f',
        '*port*', 'REPORT*', '日*語', '*語', '日本語', '\\\\', 'a\\\\*', '\\n', '\\"', '\\u{41}*', '\\0', '\\q', '\\', 'a\\', '?', '.', '.*', 'a.b']
STRS = ['', 'a', 'aa', 'ab', 'ba', 'aba', 'abab', 'ababab', 'a*b', 'a*', '*', '**', 'axb', 'axbxc', 'abc', 'cba', 'report.pdf', 'REPORT.pdf', '日本語', '日語',
        'a\\b', 'a\nb', 'A', '.', 'ab.ab', 'x y z', 'xyz', 'xzy', '\\', '"', 'a"b']
for p in PATS:
    for s in STRS:
        cases.append('"%s" like "%s"' % (esc(s), p))
suites["like"] = cases

# 8. arithmetic and misc
cases = []
NUMS = ['0', '1', '-1', '2', '3', '10', '1000000', '2147483647', '4294967296', '9007199254740992', '9007199254740993', '4611686018427387904',
        '9223372036854775807', '9223372036854775806', '-9223372036854775808', '-9223372036854775807', '3037000499', '3037000500', '-3037000499']
for a, b in itertools.product(NUMS, NUMS):
    cases += ['%s + %s' % (a, b), '%s - %s' % (a, b), '%s * %s' % (a, b), '%s < %s' % (a, b), '%s == %s' % (a, b)]
for a in NUMS:
    cases += ['-%s' % a, '-(%s)' % a, '--%s' % a, '- -%s' % a]
cases += ['9223372036854775808', '-9223372036854775809', '99999999999999999999', '1 +', '+ 1', '1 2', '()', '(1', '1)', '[1,', '{a: }', '{a 1}', '{1: 2}',
          '{"a": 1, "a": 2}', '{a: 1, a: 2}', 'if true then 1', 'if true then 1 else', 'then', '1 ? 2 : 3', '1 / 2', '1 % 2', '5 & 3', '1 ** 2', '!!true', '!!!true',
          '--1', '-(-1)', '- 1', '-1 + 2', '1 - -1', '1 --1', '(1 + 2) * 3', '1 + 2 * 3', '2 * 3 + 1', '1 < 2 == true', '1 == 1 == true', '1 < 2 < 3', 'true == false == false',
          'true && false || true', 'true || false && false', '!true || true', '!(true || true)', '1 in 2', 'principal == principal && 1 < 2',
          'context.n * context.n - context.neg', 'if context.mfa then context.n else context.neg', 'if context.n > 2 then "big" else "small"',
          '[1, 2, 3].contains(2) && !([].contains(1))', '{a: 1}.a', '{a: 1}["a"]', '{"a": {"b": [1, 2]}}.a.b.contains(2)', '[[1], [2]].contains([2])',
          '[{a: 1}].contains({a: 1})', '[[1, 2]].contains([2, 1])', '[1, 1, 1] == [1]', '[1, 2] == [2, 1]', '[[1, 2], [3]] == [[3], [2, 1]]',
          '{a: [1, 2]} == {a: [2, 1]}', '{a: 1, b: 2} == {b: 2, a: 1}', '[ip("1.1.1.1")] == [ip("1.1.1.1/32")]', '[decimal("1.0"), decimal("1.00")] == [decimal("1.0")]',
          '"a\\u{41}"', '"\\u{1F600}"', '"\\u{110000}"', '"\\u{}"', '"\\x41"', '"\\\'"', '"\\0"', '"tab\\t"', 'true', 'false', 'True', 'null', 'principal.name == "Alice"',
          'context.unicode like "日*"', 'context.empty == ""', 'context.none.isEmpty()', 'context.tags.contains("x")', 'context.ip.isInRange(ip("10.0.0.0/8"))',
          'context.dec.lessThan(decimal("2.0"))', 'context.at.toTime()', 'context.at.toDate()', 'context.at < datetime("2025-01-01")', 'context.dur > duration("1h")',
          'principal.score.lessThan(decimal("3.0"))', 'principal.score == decimal("2.5")', 'principal.addr.isInRange(ip("10.0.0.0/8"))', 'principal.since.toDate()',
          'principal.ttl.toMinutes()', 'principal.ttl == duration("90m")', 'resource.owner.manager.name', 'resource.owner == principal', 'resource.owner in Group::"all"',
          'principal.nums.containsAll([1, 3])', 'principal.tags.containsAny(["z", "b"])', 'principal.profile == {city: "Tokyo", geo: {lat: 35, lon: 139}}',
          'principal.profile.geo.lat + principal.profile.geo.lon', 'principal.getTag("level") + 1', 'principal.getTag("role") like "ad*"']
suites["arith-misc"] = cases



# ---------------------------------------------------------------- generator
OUT = os.path.join(REPO, "cedar_conformance_tests.mq")
rnd = random.Random(20260101)
workdir = tempfile.mkdtemp(prefix="cedar_conformance_")
setup(workdir)

BIG = re.compile(r"\d{16,}|\d{11,}d")  # beyond f64 precision: see README Limitations
SAMPLE = {"binary-matrix": 150, "unary-and-methods": 150, "constructors": None, "extension-compare": 100, "ip-ranges": 100,
          "datetime-duration": None, "hierarchy": 100, "attributes": 120, "like": 150, "arith-misc": None}

version = sh(["cedar", "language-version"])[1].strip()
selected = {}
for name, n in SAMPLE.items():
    cs = [c for c in suites[name] if not BIG.search(c)]
    if n and len(cs) > n:
        cs = rnd.sample(cs, n)
    selected[name] = sorted(set(cs), key=cs.index)

all_cases = [c for cs in selected.values() for c in cs]
with ThreadPoolExecutor(max_workers=8) as ex:
    expected = dict(zip(all_cases, ex.map(lambda c: cedar_eval_one(workdir, c), all_cases)))


def exp_text(c):
    r = expected[c]
    return "ERR" if "err" in r else r["ok"]


# authorization trials
PRINCIPAL_SCOPES = ['principal', 'principal == User::"alice"', 'principal == User::"bob"', 'principal in Group::"staff"', 'principal in Group::"all"',
                    'principal in User::"ghost"', 'principal is User', 'principal is Group', 'principal is User in Group::"staff"',
                    'principal is User in Group::"all"', 'principal == User::"ghost"']
ACTION_SCOPES = ['action', 'action == Action::"view"', 'action == Action::"edit"', 'action in Action::"read"',
                 'action in [Action::"view", Action::"edit"]', 'action in []']
RESOURCE_SCOPES = ['resource', 'resource == Photo::"p1"', 'resource in Album::"trip"', 'resource in Album::"all"', 'resource in Album::"work"',
                   'resource is Photo', 'resource is Album', 'resource is Photo in Album::"all"', 'resource == Photo::"ghost"']
CONDS = ['context.mfa', '!context.mfa', 'resource.public', 'principal.age >= 18', 'principal.age < 18', 'context.missing', '1 + "a"',
         'principal has admin && principal.admin', 'context.ip.isInRange(ip("10.0.0.0/8"))', 'resource.owner == principal',
         'principal in Group::"dev"', '1', 'context.tags.contains("x")', '"abc" like "a*"', 'true', 'false', 'context.n > 2',
         'principal.getTag("role") == "admin"', 'principal.hasTag("role")', 'if context.mfa then resource.public else false',
         'context.at < datetime("2025-01-01")', 'principal.manager.name == "Bob"', 'resource has owner && resource.owner in Group::"staff"',
         'context.who in Group::"staff"', 'context has nested.x.y']
CONTEXTS = [CONTEXT, dict(CONTEXT, mfa=False), {k: v for k, v in CONTEXT.items() if k not in ("ip", "tags")}, dict(CONTEXT, n=1, tags=["x"]), {}]
PRINCIPALS = ['User::"alice"', 'User::"bob"', 'User::"ghost"', 'Group::"staff"']
ACTIONS = ['Action::"view"', 'Action::"read"', 'Action::"edit"', 'Action::"other"']
RESOURCES = ['Photo::"p1"', 'Photo::"p2"', 'Photo::"ghost"', 'Album::"trip"']


def gen_policy(i):
    eff = rnd.choice(['permit', 'permit', 'permit', 'forbid'])
    ann = '@id("p%d") ' % i if rnd.random() < 0.3 else ''
    conds = ''.join(' %s { %s }' % (rnd.choice(['when', 'unless']), rnd.choice(CONDS)) for _ in range(rnd.choice([0, 0, 1, 1, 2, 3])))
    return '%s%s (%s, %s, %s)%s;' % (ann, eff, rnd.choice(PRINCIPAL_SCOPES), rnd.choice(ACTION_SCOPES), rnd.choice(RESOURCE_SCOPES), conds)


trials = []
for _ in range(120):
    pols = "\n".join(gen_policy(i) for i in range(rnd.randint(1, 5)))
    trials.append((pols, {"principal": rnd.choice(PRINCIPALS), "action": rnd.choice(ACTIONS), "resource": rnd.choice(RESOURCES),
                          "context": rnd.choice(CONTEXTS)}))


def cedar_authz(i):
    pols, req = trials[i]
    pf = os.path.join(workdir, "p_%d.cedar" % i)
    rf = os.path.join(workdir, "r_%d.json" % i)
    open(pf, "w").write(pols)
    write(rf, req)
    rc, out, err = sh(["cedar", "authorize", "--policies", pf, "--entities", os.path.join(workdir, "e.json"), "--request-json", rf, "--verbose"])
    text = out + err
    m = re.match(r"\s*(ALLOW|DENY)", text)
    assert m, text
    errs = sorted(set(re.findall(r"error while evaluating policy `([^`]+)`", text)))
    reasons = []
    if "due to the following policies:" in text:
        reasons = sorted(l.strip() for l in text.split("due to the following policies:")[1].splitlines() if l.strip())
    return {"decision": m.group(1).lower(), "reasons": reasons, "errors": errs}


with ThreadPoolExecutor(max_workers=8) as ex:
    authz = list(ex.map(cedar_authz, range(len(trials))))



# ---------------------------------------------------------------- policy parsing cases
S = 'permit (principal, action, resource)'
PARSE_CASES = [
    # structure
    '', ' ', '// c', S + ';', S, S + ' ;', 'permit(principal,action,resource);', 'forbid (principal, action, resource);',
    'Permit (principal, action, resource);', 'allow (principal, action, resource);',
    'permit (principal, action);', 'permit (principal, action, resource, context);', 'permit (action, principal, resource);',
    'permit (principal, action, resource,);', 'permit (principal,, action, resource);', 'permit ();', 'permit principal, action, resource;',
    S + '; ' + S + ';', S + ';;', S + ';\n' + S, S + ' when { true };', S + ' when { true }', S + ' when {};', S + ' when { true } unless { false };',
    S + ' unless { false } when { true };', S + ' when { true } when { true };', S + ' when true;', S + ' where { true };',
    S + ' when { true; };', S + ' when { true, false };',
    # annotations
    '@id("a") ' + S + ';', '@id ' + S + ';', '@id() ' + S + ';', '@id(a) ' + S + ';', '@id("a") @doc("b") ' + S + ';', '@id("a") @id("b") ' + S + ';',
    '@a1_b("x") ' + S + ';', '@1a("x") ' + S + ';', '@if("x") ' + S + ';', '@permit("x") ' + S + ';', '@id("a")\n@doc("b")\n' + S + ';',
    '@id("a"' + S + ';', '@ id("a") ' + S + ';', '@id ("a") ' + S + ';', '@Ns::id("a") ' + S + ';', '@id("") ' + S + ';',
    # scope
    'permit (principal == User::"a", action, resource);', 'permit (principal in User::"a", action, resource);', 'permit (principal is User, action, resource);',
    'permit (principal is User in Group::"g", action, resource);', 'permit (principal is Ns::User, action, resource);',
    'permit (principal is User in ?principal, action, resource);', 'permit (principal == ?principal, action, resource);',
    'permit (principal == ?resource, action, resource);', 'permit (principal, action, resource == ?resource);', 'permit (principal in ?principal, action, resource in ?resource);',
    'permit (principal in [User::"a"], action, resource);', 'permit (principal, action == Action::"a", resource);', 'permit (principal, action in Action::"a", resource);',
    'permit (principal, action in [Action::"a", Action::"b"], resource);', 'permit (principal, action in [], resource);', 'permit (principal, action in [Action::"a",], resource);',
    'permit (principal, action in [Action::"a" Action::"b"], resource);', 'permit (principal, action is Action, resource);', 'permit (principal, action == ?action, resource);',
    'permit (principal, action in [?action], resource);', 'permit (principal, action, resource is Photo in Album::"a");', 'permit (principal, action, resource in [Album::"a"]);',
    'permit (principal == "a", action, resource);', 'permit (principal == User, action, resource);', 'permit (principal == User::a, action, resource);',
    'permit (principal == User::"a"::"b", action, resource);', 'permit (principal == Ns::Sub::User::"a", action, resource);', 'permit (principal == _x::"a", action, resource);',
    'permit (principal != User::"a", action, resource);', 'permit (principal <= User::"a", action, resource);', 'permit (principal like "a", action, resource);',
    'permit (principal is, action, resource);', 'permit (principal is User in, action, resource);', 'permit (principal is User::"a", action, resource);',
    'permit (principal == User::"a" , action , resource );', 'permit (principal == User :: "a", action, resource);', 'permit (principal==User::"a",action,resource);',
    'permit (principal is principal, action, resource);', 'permit (principal is if, action, resource);', 'permit (principal is permit, action, resource);',
    'permit (principal == User::"a\\"b", action, resource);', 'permit (principal == User::"\\u{41}", action, resource);', 'permit (principal == User::"\\q", action, resource);',
    'permit (principal == User::\'a\', action, resource);',
    # expressions
    S + ' when { principal == User::"a" };', S + ' when { context.a.b.c };', S + ' when { context["a"] };', S + ' when { context[a] };', S + ' when { context.a[0] };',
    S + ' when { context.if };', S + ' when { context.true };', S + ' when { context.principal };', S + ' when { context.has };', S + ' when { context.in };',
    S + ' when { context.like };', S + ' when { context.is };', S + ' when { context.permit };', S + ' when { context.then };', S + ' when { context has if };',
    S + ' when { context has "a b" };', S + ' when { context has a.b };', S + ' when { context has a.if };', S + ' when { context has a. };', S + ' when { context has };',
    S + ' when { context has 1 };', S + ' when { context has a["b"] };', S + ' when { context has principal };', S + ' when { context has true };',
    S + ' when { 1 + 2 * 3 - 4 };', S + ' when { -1 };', S + ' when { - 1 };', S + ' when { --1 };', S + ' when { !-1 };', S + ' when { -(1) };', S + ' when { 1 - -1 };',
    S + ' when { 1 +- 1 };', S + ' when { 9223372036854775807 };', S + ' when { 9223372036854775808 };', S + ' when { -9223372036854775808 };', S + ' when { -9223372036854775809 };',
    S + ' when { 99999999999999999999999 };', S + ' when { 01 };', S + ' when { 0x10 };', S + ' when { 1.5 };', S + ' when { 1e3 };', S + ' when { 1_000 };',
    S + ' when { 1 / 2 };', S + ' when { 1 % 2 };', S + ' when { 1 & 2 };', S + ' when { 1 ^ 2 };', S + ' when { 1 << 2 };',
    S + ' when { true && false || true };', S + ' when { true and false };', S + ' when { true or false };', S + ' when { not true };', S + ' when { !true };',
    S + ' when { a == b == c };', S + ' when { 1 < 2 < 3 };', S + ' when { 1 < 2 == true };', S + ' when { 1 == 2 < 3 };', S + ' when { 1 in 2 in 3 };',
    S + ' when { 1 in 2 == true };', S + ' when { 1 + 2 in 3 };', S + ' when { 1 like "a" like "b" };', S + ' when { principal has a has b };',
    S + ' when { principal is User is User };', S + ' when { principal is User && true };', S + ' when { principal is User in Group::"a" && true };',
    S + ' when { principal is User in Group::"a" in Group::"b" };', S + ' when { principal is User == true };', S + ' when { 1 + principal is User };',
    S + ' when { if true then 1 else 2 };', S + ' when { if true then 1 };', S + ' when { if true then 1 else 2 + 3 };', S + ' when { 1 + if true then 1 else 2 };',
    S + ' when { (if true then 1 else 2) + 1 };', S + ' when { if true then if false then 1 else 2 else 3 };', S + ' when { if true then 1 else 2 == 2 };',
    S + ' when { if true then 1 else 2 else 3 };', S + ' when { true && if true then true else false };', S + ' when { !if true then true else false };',
    S + ' when { [1, 2, 3] };', S + ' when { [] };', S + ' when { [1,] };', S + ' when { [,] };', S + ' when { [1 2] };', S + ' when { [[1], [2]] };',
    S + ' when { {a: 1} };', S + ' when { {"a": 1} };', S + ' when { {} };', S + ' when { {a: 1,} };', S + ' when { {a: 1, a: 2} };', S + ' when { {a: 1, "a": 2} };',
    S + ' when { {1: 2} };', S + ' when { {if: 1} };', S + ' when { {true: 1} };', S + ' when { {a.b: 1} };', S + ' when { {a: 1}.a };', S + ' when { {a: 1}["a"] };',
    S + ' when { [1].contains(1) };', S + ' when { [1].contains() };', S + ' when { [1].contains(1, 2) };', S + ' when { [1].isEmpty() };', S + ' when { [1].isEmpty(1) };',
    S + ' when { [1].foo(1) };', S + ' when { context.a.foo() };', S + ' when { context.a.contains(1).b };', S + ' when { context.a.contains(1).contains(2) };',
    S + ' when { context.a(1) };', S + ' when { contains(1) };', S + ' when { ip("1.1.1.1") };', S + ' when { ip("1.1.1.1").isIpv4() };', S + ' when { ip() };',
    S + ' when { ip("a", "b") };', S + ' when { decimal("1.0").lessThan(decimal("2.0")) };', S + ' when { Ns::fn(1) };', S + ' when { fn(1) };', S + ' when { ip };',
    S + ' when { ip("1.1.1.1").isInRange(ip("1.0.0.0/8")) };', S + ' when { datetime("2024-01-01").toDate() };', S + ' when { duration("1h").toHours() };',
    S + ' when { User::"a" };', S + ' when { User::"a".name };', S + ' when { User::"a" in Group::"b" };', S + ' when { Ns::User::"a" };', S + ' when { User::"a"::"b" };',
    S + ' when { User:: "a" };', S + ' when { User ::"a" };', S + ' when { User };', S + ' when { Ns::User };', S + ' when { Ns::User.name };',
    S + ' when { principal.name };', S + ' when { principal . name };', S + ' when { principal.\nname };', S + ' when { principal // c\n.name };',
    S + ' when { action == Action::"a" };', S + ' when { resource };', S + ' when { context };', S + ' when { principal };', S + ' when { Principal };',
    S + ' when { ?principal == principal };', S + ' when { principal == ?principal };', S + ' when { ?resource };', S + ' when { ?foo };',
    S + ' when { "a" like "a*" };', S + ' when { "a" like "a\\*" };', S + ' when { "a" like "a\\**" };', S + ' when { "a" like a };', S + ' when { "a" like "\\q" };',
    S + ' when { "a" like "\\u{41}*" };', S + ' when { "a" like "a**b" };', S + ' when { "a" like "*" };', S + ' when { "a*" == "a\\*" };', S + ' when { "a\\*" == "a" };',
    S + ' when { "\\n\\r\\t\\\\\\0\\"\\\'" };', S + ' when { "\\x41" };', S + ' when { "\\x80" };', S + ' when { "\\u{0}" };', S + ' when { "\\u{110000}" };', S + ' when { "\\u{D800}" };',
    S + ' when { "multi\nline" };', S + ' when { "tab\there" };', S + ' when { "unterminated };', S + ' when { \'single\' };', S + ' when { "日本語" };', S + ' when { "😀" };',
    S + ' when { context.日本語 };', S + ' when { context.a-b };', S + ' when { context.a$b };', S + ' when { context._a };', S + ' when { context.a1 };', S + ' when { context.1a };',
    S + ' when { true } // trailing', S + ' // c\nwhen { true };', S + ' /* c */ when { true };', '/* c */ ' + S + ';', S + ' when { true /* c */ };',
    S + ' when { context.a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p };', S + ' when { ((((((((((1)))))))))) };', S + ' when { ( };', S + ' when { ) };', S + ' when { () };',
    S + ' when { principal in [User::"a", Group::"b"] };', S + ' when { principal in [] };', S + ' when { principal in User::"a" || principal in User::"b" };',
    S + ' when { context.a == "x" && (context.b || context.c) };', S + ' when { !(context.a) };', S + ' when { !!context.a };', S + ' when { !context.a.b };',
    S + ' when { -context.a };', S + ' when { context.a * -1 };', S + ' when { 1 * 2 * 3 };', S + ' when { 1 - 2 - 3 };', S + ' when { 1 * (2 + 3) };',
    S + ' when { principal == User::"a" } unless { resource == Photo::"b" } when { context.c };',
    S + ' when { context.permit };', S + ' when { context.forbid };', S + ' when { context.when };', S + ' when { context.unless };', S + ' when { context.else };',
    S + ' when { context has when };', S + ' when { context has else };', S + ' when { context has unless };', S + ' when { context has forbid };',
    S + ' when { context.a.has };', S + ' when { context["has"] };', S + ' when { {has: 1} };', S + ' when { {if: 1} };', S + ' when { {principal: 1} };',
    S + ' when { {context: 1}.context };', S + ' when { {"if": 1}["if"] };', S + ' when { context.a is_ };',
]
PARSE_CASES = list(dict.fromkeys(PARSE_CASES))


def norm_has(n):
    """Expands `has` with an attribute path into nested single-attribute `has` tests (the form cedar.mq emits)."""
    if isinstance(n, list):
        return [norm_has(x) for x in n]
    if not isinstance(n, dict):
        return n
    out = {k: norm_has(v) for k, v in n.items()}
    if set(out) == {"has"} and isinstance(out["has"].get("attr"), list):
        left, attrs = out["has"]["left"], out["has"]["attr"]
        acc = {"has": {"left": left, "attr": attrs[0]}}
        base = {".": {"left": left, "attr": attrs[0]}}
        for a in attrs[1:]:
            acc = {"&&": {"left": acc, "right": {"has": {"left": base, "attr": a}}}}
            base = {".": {"left": base, "attr": a}}
        return acc
    return out


def cedar_policy_json(text):
    """Cedar's JSON policy for a one-policy text, or None when Cedar rejects it."""
    p = subprocess.run(["cedar", "translate-policy", "--direction", "cedar-to-json"], input=text, capture_output=True, text=True)
    if p.returncode != 0:
        return None
    d = json.loads(p.stdout)
    pols = list(d.get("staticPolicies", {}).values()) + list(d.get("templates", {}).values())
    if len(pols) != 1:
        return None
    pol = norm_has(pols[0])
    pol.setdefault("annotations", {})
    return pol


def cedar_accepts(text):
    with tempfile.NamedTemporaryFile("w", suffix=".cedar", delete=False, dir=workdir) as f:
        f.write(text)
    return sh(["cedar", "check-parse", "--policies", f.name])[0] == 0


with ThreadPoolExecutor(max_workers=8) as ex:
    parse_accept = list(ex.map(cedar_accepts, PARSE_CASES))
    parse_json = list(ex.map(lambda t: cedar_policy_json(t) if not BIG.search(t) else None, PARSE_CASES))


def lit(v):
    return json.dumps(v, ensure_ascii=False)


def mq_lit(v):
    """Like lit, but writes null as mq's None."""
    if v is None:
        return "None"
    if isinstance(v, list):
        return "[" + ", ".join(mq_lit(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join("%s: %s" % (json.dumps(k, ensure_ascii=False), mq_lit(x)) for k, x in v.items()) + "}"
    return lit(v)


lines = []
w = lines.append
w("# Conformance tests generated by scripts/gen_conformance_tests.py (needs the Cedar CLI). Do not edit by hand.")
w("# Expected values are the output of the Cedar CLI (`cedar evaluate` / `cedar authorize`), %s." % version.replace("Cedar language version: ", "Cedar language version "))
w("# \"ERR\" means the Cedar CLI reported an error, which cedar.mq must report as an error too.")
w("")
w('include "test"')
w("| include \"cedar\"")
w("|")
w("")
w("def _conf_request():")
w("  " + lit(REQUEST_CEDAR))
w("end")
w("")
w("def _conf_entities():")
w("  " + lit(ENTITIES))
w("end")
w("")
w("def _conf_ok(c, req, ents):")
w("  if (c[1] == \"ERR\"):")
w("    try: do cedar_eval(c[0], req, ents) | false end catch(e): starts_with(e[\"message\"], \"cedar: \")")
w("  else:")
w("    try: cedar_eval(\"(\" + c[0] + \") == (\" + c[1] + \")\", req, ents) == true catch(e): false")
w("end")
w("")
w("# Returns the expressions whose result differs from the Cedar CLI's.")
w("def _conf_failures(cases):")
w("  let req = _conf_request()")
w("  | let ents = _conf_entities()")
w("  | map(filter(cases, fn(c): !_conf_ok(c, req, ents);), fn(c): c[0];)")
w("end")
w("")

CHUNK = 200
for name, cs in selected.items():
    ident = name.replace("-", "_")
    for k in range(0, len(cs), CHUNK):
        part = cs[k:k + CHUNK]
        suffix = "" if len(cs) <= CHUNK else "_%d" % (k // CHUNK + 1)
        w("def test_conformance_%s%s():" % (ident, suffix))
        w("  let failures = _conf_failures([")
        for i, c in enumerate(part):
            w("    [%s, %s]%s" % (lit(c), lit(exp_text(c)), "," if i < len(part) - 1 else ""))
        w("  ])")
        w("  | assert_eq(failures, [])")
        w("end")
        w("")

# authorization
w("def _conf_authz_failures(trials):")
w("  let ents = _conf_entities()")
w("  | map(filter(trials, fn(t): !_conf_authz_ok(t, ents);), fn(t): t[0];)")
w("end")
w("")
w("def _conf_authz_ok(t, ents):")
w("  try:")
w("    do")
w("      let r = cedar_is_authorized(t[0], t[1], ents)")
w("      | r[\"decision\"] == t[2][\"decision\"] && sort(r[\"reasons\"]) == t[2][\"reasons\"]")
w("        && sort(map(r[\"errors\"], fn(e): e[\"policy\"];)) == t[2][\"errors\"]")
w("    end")
w("  catch(e): false")
w("end")
w("")
for k in range(0, len(trials), 60):
    part = list(range(k, min(k + 60, len(trials))))
    w("def test_conformance_authorization_%d():" % (k // 60 + 1))
    w("  let failures = _conf_authz_failures([")
    for j, i in enumerate(part):
        w("    [%s, %s, %s]%s" % (lit(trials[i][0]), lit(trials[i][1]), lit(authz[i]), "," if j < len(part) - 1 else ""))
    w("  ])")
    w("  | assert_eq(failures, [])")
    w("end")
    w("")


# policy parsing
w("def _conf_parses(text):")
w("  try: do cedar_parse(text) | true end catch(e): false")
w("end")
w("")
w("# Policy texts and whether the Cedar CLI accepts them.")
w("def test_conformance_parse_acceptance():")
w("  let failures = map(filter([")
for i, (t, ok) in enumerate(zip(PARSE_CASES, parse_accept)):
    w("    [%s, %s]%s" % (lit(t), lit(ok), "," if i < len(PARSE_CASES) - 1 else ""))
w("  ], fn(c): _conf_parses(c[0]) != c[1];), fn(c): c[0];)")
w("  | assert_eq(failures, [])")
w("end")
w("")
ast_cases = [(t, j) for t, j in zip(PARSE_CASES, parse_json) if j is not None]
w("# Policy texts and the Cedar CLI's JSON policy for them (`has` paths expanded, as cedar_parse does).")
w("def test_conformance_parse_ast():")
w("  let failures = map(filter([")
for i, (t, j) in enumerate(ast_cases):
    w("    [%s, %s]%s" % (lit(t), mq_lit(j), "," if i < len(ast_cases) - 1 else ""))
w("  ], fn(c): try: cedar_parse(c[0]) != [c[1]] catch(e): true;), fn(c): c[0];)")
w("  | assert_eq(failures, [])")
w("end")
w("")
open(OUT, "w").write("\n".join(lines))
print("wrote %s: %d expression cases, %d authorization trials, %d parsing cases (%d with ASTs), %d bytes" % (OUT, len(all_cases), len(trials), len(PARSE_CASES), len(ast_cases), os.path.getsize(OUT)))
