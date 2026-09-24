# Shadow

Shadow is an Instagram conversation agent under development. The intended product learns an owner's conversational preferences, remembers relevant context, replies through Instagram Web, and accepts private instructions through Telegram.

**Status: prototype runtime; implementation specification ready, production gates not passed.** No claim of reliable unattended operation, guaranteed delivery, or human indistinguishability is made.

Start with [the implementation plan](plan/plan.md). It links the requirements, design, ordered phases, tests, and copyable execution prompts for implementing the next version.

The existing root-level design documents describe the earlier prototype. For new implementation work, `plan/` is authoritative. Preserve the older documents as historical input until the release privacy review decides what can be published.

## Publication warning

This checkout has historically tracked private conversations and derived data. Ignore rules do not remove tracked files or Git history. **Do not publish this checkout or its history before completing [the privacy release gate](plan/privacy.md).** Public examples and tests must use fictional data.

## Current development check

```sh
python test_shadow.py
```

This legacy suite currently uses local private data. It is not the future clean-clone test contract. See [testing](plan/testing.md) for that transition.

## Intended distribution

One Instagram account and one owner per self-hosted installation; no shared hosted service. Python remains the backend. Instagram access uses the rendered DOM through a persistent browser session, without official or unofficial Instagram API clients. Cloud model APIs and Telegram's Bot API are separate permitted integrations.

The intended release license is dual **MIT OR Apache-2.0**. The release phase must add and verify both license texts and copyright ownership; this statement alone does not install those licenses.
