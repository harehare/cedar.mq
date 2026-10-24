<h1 align="center">cedar.mq</h1>

A [Cedar](https://www.cedarpolicy.com/) policy language parser implemented as an [mq](https://github.com/harehare/mq) module.

Parses Cedar policy text into the [Cedar JSON policy format](https://docs.cedarpolicy.com/policies/json-format.html), so the result can be queried with mq or passed to other Cedar tooling. This module does **not** evaluate or validate policies against a schema.

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

Each policy has the keys `annotations`, `effect`, `principal`, `action`, `resource` and `conditions`. `annotations` is always present (empty when there are none).

Raises an error with the line number on invalid syntax.

### `cedar_stringify(data)`

Serializes a policy, or an array of policies, in the Cedar JSON policy format to Cedar policy text. Policies are separated by a blank line and parentheses are added only where precedence requires them. Comments are not preserved.

| Input type | Output |
|---|---|
| Array or dict | Cedar policy text (string) |

Extension functions `ip`, `decimal`, `datetime`, `duration` and namespaced names are printed as `name(args)`; any other extension function is printed as a method call on its first argument (`isInRange`, `lessThan`, ...). Raises an error on an unsupported expression or scope operator.

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

## Limitations

- Syntax only: no schema validation, type checking or authorization evaluation.
- `a has b.c` is expanded to `(a has b) && (a.b has c)` as in the JSON policy format.
- Identifiers that are not variables, entity references or function calls are rejected.

## Compatibility

Requires [mq](https://github.com/harehare/mq) v0.9 or later.

## License

MIT
