# Project invariants

## Telegram and MAX parity

The product has Telegram and MAX messenger implementations.

- When the user asks to add, change, or fix product behavior, apply the change to both messenger implementations by default.
- Treat a change as platform-specific only when the user explicitly says it should affect Telegram only or MAX only.
- Keep shared domain behavior consistent while using platform-native adapters, authentication, buttons, web bridges, and delivery mechanisms.
- Verify both implementations after shared behavior changes.
- Never delete production data or modify unrelated hosted sites without explicit user confirmation.
