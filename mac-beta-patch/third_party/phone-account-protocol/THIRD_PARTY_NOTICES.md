# Account protocol dependencies and provenance

The unmodified MIT components `vendor/TTEncrypt.py`, `vendor/XGorgon.py`,
`vendor/XLadon.py`, and `vendor/pkcs7_padding.py` are from
[ssovit/x-gorogn-khronos-argus-ladon](https://github.com/ssovit/x-gorogn-khronos-argus-ladon),
commit `9ef4ab947916b8a37de0ac1b8fd712dac5feae42`, original `lib/` directory.
The complete copyright and permission notice is retained in `vendor/LICENSE.ssovit`.
File hashes are pinned in `protocol-integrity.json`. Preserve both notices.
PyCryptodome is reused from the existing packaged runtime for ARC4/AES primitives.

The 8404 framing adapter, Medusa matrix/digest/serialization, request transport,
account state machine, private child-process bridge and UI are independently
written. `medusa_wire_tables.json` contains public protocol numeric parameters,
not executable competitor code. Protocol constants and byte layouts were checked
with synthetic inputs using an offline interoperability oracle over the separately
retained reference binary. Synthetic vectors contain no real identities or tokens.
The oracle, emulator and reference binary are not product dependencies and are
never packaged. No competitor account identity, Cookie, session token, assets,
branding or UI implementation is reused.

Our own registered device context is protected by Windows DPAPI and kept outside
the application package. When "keep me signed in" is enabled, the user's session
token and cookies are also protected by Windows DPAPI in the local application
data directory, bound to the registered device and limited to 30 days locally.
The server may invalidate the session earlier. Disabling that option or signing
out removes the saved credentials. SMS codes and MFA challenges are not persisted.
Offline vector agreement is not a claim of successful real SMS delivery or phone
library synchronization; these require explicit user login and acceptance.
