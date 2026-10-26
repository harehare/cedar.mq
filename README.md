<h1 align="center">cedar.mq</h1>

A [Cedar](https://www.cedarpolicy.com/) policy language parser implemented as an [mq](https://github.com/harehare/mq) module.

Parses Cedar policy text into the [Cedar JSON policy format](https://docs.cedarpolicy.com/policies/json-format.html), so the result can be queried with mq or passed to other Cedar tooling. It can also check whether a request matches the policies (`cedar_is_authorized`) and evaluate single expressions (`cedar_eval`). This module does **not** validate policies against a schema.

## Features

- `permit` / `forbid` policies with `when` / `unless` conditions
- Scope constraints: `==`, `in`, `is`, `is ... in`, action sets (`action in [...]`), and `?principal` / `?resource` template slots
- Annotations (`@id("name")`, `@flag`)
- Operators: `||`, `&&`, `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `has`, `like`, `is`, `+`, `-`, `*`, `!`, unary `-`
- `if ... then ... else ...`
- Attribute access (`a.b`, `a["b"]`) and `has` paths (`a has b.c`)
- Set and record literals, namespaced entity references (`App::User::"alice"`)
- Methods (`contains`, `containsAll`, `containsAny`, `isEmpty`, `getTag`, `hasTag`) and extension functions (`ip`, `decimal`, `isInRange`, ...)
- `//` comments
- `cedar_stringify` for the reverse direction (JSON policy format to policy text)
- `cedar_is_authorized`: evaluates policies against a request and entities (scope, `when` / `unless`, entity hierarchy, tags, extension types) and returns `allow` / `deny` with the determining policies, following the [Cedar authorization rules](https://docs.cedarpolicy.com/auth/authorization.html)
- `cedar_eval` for a single expression and `cedar_link` for instantiating policy templates

## Installation

Copy `cedar.mq` to your mq module directory, or place it anywhere and reference it with `-L`.

```sh
cp cedar.mq ~/.local/mq/config/
```

### HTTP Import (no local installation needed)

HTTP imports are disabled by default; pass `--allow-http-import` to import directly from GitHub without any local setup:

```sh
mq --allow-http-import -I raw 'import "github.com/harehare/cedar.mq" | cedar::cedar_parse(.)' policies.cedar
```

Pin to a specific release with `@vX.Y.Z`:

```sh
mq --allow-http-import -I raw 'import "github.com/harehare/cedar.mq@v0.1.0" | cedar::cedar_parse(.)' policies.cedar
```

## Usage

```sh
mq -L /path/to/modules -I raw \
  'import "cedar" | cedar::cedar_parse(.)' policies.cedar
```

If you copied it to the mq built-in module directory:

```sh
mq -I raw 'import "cedar" | cedar::cedar_parse(.)' policies.cedar
```

## API

### `cedar_parse(input)`

Parses Cedar policy text and returns an array with one entry per policy.

| Input type | Output |
|---|---|
| String | Array of policies in the Cedar JSON policy format |

Each policy has the keys `annotations`, `effect`, `principal`, `action`, `resource` and `conditions`. `annotations` is always present (empty when there are none). The output matches what `cedar translate-policy --direction cedar-to-json` produces, with these notes:

- An annotation without a value (`@flag`) is `null`; `@flag("")` is the empty string.
- `like` patterns have one `{"Literal": "c"}` element per character.
- `action in [X]` with a single element is the `"entity"` form of the scope.
- `a has b.c` is expanded to `(a has b) && (a.b has c)` (Cedar 4.14 prints `"attr": ["b", "c"]`; both mean the same).

Raises an error with the line number on invalid syntax. The accepted syntax follows Cedar: trailing commas are allowed in the scope, sets, records and action lists; reserved words (`true`, `false`, `if`, `then`, `else`, `in`, `is`, `like`, `has`) cannot be used as attribute names, record keys or type names unless quoted; integer literals must fit in a `long`; `!` and `-` cannot be chained together (`!-1`); template slots are only allowed in the scope (`?principal`, `?resource`); duplicate annotations and duplicate record keys are rejected; and only the known functions and methods can be called (with the right number of arguments for methods).

### `cedar_stringify(data)`

Serializes a policy, or an array of policies, in the Cedar JSON policy format to Cedar policy text. Policies are separated by a blank line and parentheses are added only where precedence requires them. Comments are not preserved.

| Input type | Output |
|---|---|
| Array or dict | Cedar policy text (string) |

Extension functions `ip`, `decimal`, `datetime`, `duration` and namespaced names are printed as `name(args)`; any other extension function is printed as a method call on its first argument (`isInRange`, `lessThan`, ...). Raises an error on an unsupported expression or scope operator.

### `cedar_is_authorized(policies, request, entities)`

Evaluates every policy against the request and applies the Cedar decision rules: a satisfied `forbid` denies, otherwise a satisfied `permit` allows, otherwise the request is denied. A policy whose evaluation fails (missing attribute, type error, overflow, ...) is skipped and reported in `errors`.

| Argument | Description |
|---|---|
| `policies` | Cedar policy text, one policy, or an array of policies in the Cedar JSON policy format |
| `request` | `{"principal": {"type", "id"}, "action": {"type", "id"}, "resource": {"type", "id"}, "context": {...}}`. Entity references may also be written as `{"__entity": {...}}` or `Type::"id"` text. `context` defaults to `{}` |
| `entities` | Array of `{"uid", "attrs", "parents", "tags"}` in the [Cedar JSON entity format](https://docs.cedarpolicy.com/auth/entities-syntax.html). Use `[]` for none |

Returns `{"decision": "allow" | "deny", "reasons": [...], "errors": [...]}`. `reasons` lists the ids of the determining policies: the satisfied `permit` policies on allow, the satisfied `forbid` policies on a deny caused by `forbid`, and nothing on a default deny. A policy's id is its `@id` annotation, or `policy<index>` (zero-based) otherwise. Each entry of `errors` is `{"policy": id, "message": ...}`.

Values in `context` and `attrs` use the Cedar JSON value format: records are dicts, sets are arrays, entities are `{"__entity": {"type", "id"}}` and extension values are `{"__extn": {"fn", "arg"}}`, e.g. `{"__extn": {"fn": "ip", "arg": "10.0.0.5"}}`.

Supported: every operator and method listed under Features, the entity hierarchy (`in` follows `parents` transitively, including cycles), `getTag` / `hasTag`, and the extension types `ip`, `decimal`, `datetime` and `duration` with all their methods (`isIpv4`, `isIpv6`, `isLoopback`, `isMulticast`, `isInRange`, `lessThan` / `lessThanOrEqual` / `greaterThan` / `greaterThanOrEqual`, `offset`, `durationSince`, `toDate`, `toTime`, `toMilliseconds`, `toSeconds`, `toMinutes`, `toHours`, `toDays`). `<`, `<=`, `>`, `>=` also work on `datetime` and `duration`.

Unlinked policy templates (slots in the scope) are reported in `errors`; instantiate them with `cedar_link` first.

### `cedar_eval(expr, request, entities)`

Evaluates one expression and returns its value, or raises an error when evaluation fails. `expr` is Cedar expression text or an expression in the Cedar JSON policy format; `request` and `entities` are as above (`request` keys may be omitted, but then the variables they bind are unavailable).

### `cedar_link(policy, slots)`

Instantiates a policy template, given in the Cedar JSON policy format, and returns the linked policy. `slots` maps slot names to entity references, e.g. `{"?principal": {"type": "User", "id": "alice"}}`. Raises an error when a slot is missing.

## Example

Given `policies.cedar`:

```cedar
@id("view-photos")
permit (
  principal in Group::"viewers",
  action in [Action::"view", Action::"list"],
  resource is Photo in Album::"trip"
)
when { resource.public || principal == resource.owner };

forbid (principal, action, resource)
unless { context.mfa };
```

```sh
mq -L . -I raw 'import "cedar" | cedar::cedar_parse(.) | map(., fn(p): p["effect"];)' policies.cedar
# => ["permit", "forbid"]

mq -L . -I raw 'import "cedar" | cedar::cedar_parse(.) | first(.) | ."annotations"."id"' policies.cedar
# => view-photos

mq -L . -I raw 'import "cedar" | cedar::cedar_parse(.) | first(.) | ."action"' policies.cedar
# => {"op": "in", "entities": [{"type": "Action", "id": "view"}, {"type": "Action", "id": "list"}]}
```

Round trip:

```sh
mq -L . -I raw 'import "cedar" | cedar::cedar_parse(.) | cedar::cedar_stringify(.)' policies.cedar
```

Authorization:

```sh
mq -L . -I raw 'import "cedar" | cedar::cedar_is_authorized(., {
  "principal": {"type": "User", "id": "alice"},
  "action": {"type": "Action", "id": "view"},
  "resource": {"type": "Photo", "id": "p1"},
  "context": {"mfa": true}
}, [
  {"uid": {"type": "User", "id": "alice"}, "parents": [{"type": "Group", "id": "viewers"}]},
  {"uid": {"type": "Photo", "id": "p1"}, "attrs": {"public": true}, "parents": [{"type": "Album", "id": "trip"}]}
])' policies.cedar
# => {"decision": "allow", "reasons": ["view-photos"], "errors": []}
```

Expressions:

```sh
mq -L . -I null 'import "cedar" | cedar::cedar_eval("ip(\"10.1.2.3\").isInRange(ip(\"10.0.0.0/8\"))", {}, [])'
# => true
```

## Limitations

- No schema validation or type checking. Evaluation follows the runtime semantics only, so a policy the validator would reject may still evaluate (or fail at evaluation).
- mq numbers are 64-bit floats. Integers above 2^53 lose precision, in evaluation and also in the integer literals `cedar_parse` returns (`9223372036854775807` comes back as `9223372036854775808`). Arithmetic results at or beyond ±2^63 are reported as overflow, since the exact `long` limits are not representable. Out-of-range literals are still rejected correctly because the digits are checked as text.
- `datetime` values outside years 0000-9999 (reachable only through `offset`) are returned as `{"__extn": {"fn": "datetime", "arg": "<milliseconds since the epoch>ms"}}`.
- Entities that are not listed in `entities` are treated as having no attributes, tags or parents: `in` only matches themselves, `has` and `hasTag` are `false`, and attribute or tag access raises an error.
- Identifiers that are not variables, entity references or function calls are rejected.

## Testing

```sh
mq-test cedar_tests.mq cedar_conformance_tests.mq
```

`cedar_tests.mq` holds the unit tests. `cedar_conformance_tests.mq` is generated by `scripts/gen_conformance_tests.py` from the output of the [Cedar CLI](https://crates.io/crates/cedar-policy-cli) (4.14, Cedar language version 4.6): the expected values are what `cedar evaluate`, `cedar authorize`, `cedar check-parse` and `cedar translate-policy` print for the same inputs. To regenerate it, install `cedar` (`cargo install cedar-policy-cli`) and run the script; the cases are seeded, so the output is reproducible.

## Compatibility

Requires [mq](https://github.com/harehare/mq) v0.9 or later.

## License

MIT
