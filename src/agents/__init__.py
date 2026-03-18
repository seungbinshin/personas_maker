"""Compatibility shims for legacy prompt imports.

Layer role:
- `src/agents/*` is no longer the source of truth for prompt content.
- `prompts/*` owns the canonical prompt assets.
- `src/agents/*` exists to preserve backward-compatible imports from older
  pipeline or adapter code.

Repository layering:
- `agents` = compatibility shim
- `prompts` = prompt asset source of truth
- `skills` = reusable business logic
- `tools` = runtime / API facade

Contributor rules:
- Add or edit prompt text in `prompts/*`, not in `src/agents/*`.
- Keep `src/agents/*` as thin re-export modules only.
- Do not place workflow logic, parsing logic, or runtime calls in this package.

Allowed dependency direction:
- `src/agents/* -> prompts/*`

Forbidden examples:
- `src/agents/* -> tools/*`
- `src/agents/* -> skills/*`
- defining new prompt strings directly in `src/agents/*`
"""
