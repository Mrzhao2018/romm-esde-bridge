# Multi-device and multi-user deployment

## Data model

| Resource | Same user, different devices | Different users |
|---|---|---|
| ROMs, firmware, metadata, artwork | Shared | Shared |
| Downloads | Separate bounded device caches | Separate bounded device caches |
| Favorites and hidden flags | Synchronized | Isolated |
| States and state screenshots | Synchronized with conflict archives | Isolated |
| Play sessions and progress | Combined for the user | Isolated |
| RomM Device identity | One record per installation | One record per installation/user |

RomM stores the game library once. Adding users or devices never duplicates the
server's ROM files.

## Adding another device for the same user

Create or pair a separate RomM Client Token for that device. Do not copy the
first device's SQLite database or `client_instance_id`. Deploy the same shared
Bridge URL and server URL; the new client registers its own RomM Device and
pulls the existing user's favorites, hidden flags, states and screenshots.

## Adding a different user

1. Create the user in RomM.
2. Assign the `Viewer (legacy)` permission group. It permits reading and
   downloading the shared library and writing only that user's own collections,
   assets, devices and per-ROM data.
3. Create or pair a Client Token while signed in as that user.
4. Install the client with that token. The shared Bridge manifest, ROM library,
   firmware and media require no copy or rescan.

Use `Editor` only when the person should be allowed to change or delete the
global library. A user without a library-read permission group keeps personal
API scopes but cannot see the shared games.

One client `data_dir` is permanently bound to one RomM `user_id`. If multiple
RomM users share a single operating-system account, give each profile separate
`data_dir`, `gamelist_path`, `state_dir`, `stub_dir` and token file. Separate
physical devices already satisfy this naturally.

## State concurrency

State commits acquire a short Bridge lease keyed by authenticated
`(user_id, rom_id)`. Under that lease the client re-reads the canonical state
and screenshot revision. If both the server and local baseline changed, it
uploads the prior server pair as a timestamped archival state before committing
the local canonical pair. Different users have different lock keys and never
see or overwrite each other's states.

