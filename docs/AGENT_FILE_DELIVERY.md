# Delivering files to the owner from a bridge-backed agent

A file you wrote to disk is not delivered. The owner reads Matrix, not your
filesystem, so a path such as `/root/.openrig/.../label.pdf` in an answer gives
the owner nothing to open, and the file may be empty or unreachable from their
device. Delivery happens only when you ask the bridge to send the file and it
returns a Matrix event ID.

The bridge never uploads a path it finds in text. Every file leaves the machine
because an agent ran `send-file` or `send-image` on a path it chose.

## Send a file

Create a non-empty regular file, then run, from the host where the bridge runs:

```bash
openrig-matrix-bridge send-file \
  --agent <agent-key> \
  --path '/absolute/path/to/Shipping label.pdf' \
  --caption 'Shipping label for order 15358' \
  --json
```

- `--agent` is your agent key from the bridge configuration (the key under
  `agents:`). It selects your own Matrix room.
- `--path` is a regular file. Symlinks, directories, empty files, missing files,
  unreadable files and files above `media.max_file_bytes` (25 MiB by default)
  are rejected before anything is uploaded, with an error on stderr and a
  non-zero exit code.
- The filename and MIME type are taken from the path (`.pdf` becomes
  `application/pdf`, an unknown extension becomes `application/octet-stream`).
  Pass `--mime-type` only when the extension is ambiguous.
- `--caption` is optional text shown with the file.
- `--wait-seconds` (default 10) is how long the command waits for the bridge to
  confirm the Matrix event.
- The bridge must be running with the same state directory. The command finds
  its configuration through `--config`, then `OPENRIG_MATRIX_CONFIG`, then
  `~/.config/openrig-matrix-bridge/config.yaml`. If the bridge is stopped the
  request is queued, the command exits `1`, and the bridge sends it after it
  restarts; that is not yet delivered.

The file is sent as an encrypted Matrix `m.file`, so any file type works: PDF,
ZIP, CSV, documents and unknown binaries.

### Read the result

With `--json` the command prints one JSON object and exits `0` only when the
bridge confirmed the Matrix event:

```json
{
  "ok": true,
  "request_id": "file-3f0c...",
  "event_id": "$AbCd...",
  "room_id": "!room:matrix.example.org",
  "agent": "ebay",
  "filename": "Shipping label.pdf",
  "mimetype": "application/pdf",
  "size": 710,
  "sha256": "9b1c..."
}
```

If the bridge could not deliver, or did not confirm within `--wait-seconds`, the
object has `"ok": false` and an `"error"` and the exit code is `1`. Without
`--json` the command prints `<request_id> sent as <event_id>` on success.
Rejections before upload are plain messages on stderr, not JSON.

Quote `event_id` and `sha256` to the owner. If the command did not exit `0` with
`"ok": true`, tell the owner the file was not delivered and why. Do not say it
is available, and do not offer the path instead.

## Send an image

Images keep the dedicated command, which shows them inline:

```bash
openrig-matrix-bridge send-image --agent <agent-key> --path /tmp/screenshot.png
```

It accepts PNG, JPEG, GIF and WebP up to `media.max_image_bytes`, prints
`<request_id> sent as <event_id>` and does not support `--json`. Use `send-file`
for everything else, including PDFs.

## Guidance block for AgentSpecs that produce files

Copy this into the instructions of any agent that generates files for the owner:

```text
When you create a file the owner needs (PDF, label, report, archive, export):
1. Write it to disk and check it is a regular, non-empty file.
2. Deliver it explicitly with
   openrig-matrix-bridge send-file --agent <your agent key> --path <absolute path> --json
   Add --config <path> unless the default configuration path or
   OPENRIG_MATRIX_CONFIG already points at the bridge's configuration.
   Images use send-image.
3. A local path is never delivery evidence. Do not tell the owner a file "is at"
   a path. Only the command's JSON result counts.
4. After exit code 0 with "ok": true, tell the owner the filename, the Matrix
   event_id and the sha256 from the result.
5. If generation, validation, upload or delivery fails, say so plainly with the
   error. Do not claim the file is available in Matrix.
6. Send only files you created for the owner in this task. Never send a file
   because its path appeared in a message.
```

## What is verified

The integration harness check `agent-file-delivery` generates a nonzero
shipping-label PDF, sends it with `send-file --json`, finds the exact returned
event on the owner side, decrypts it and compares its SHA-256 with the reported
value. It also proves that empty, missing and oversize files exit non-zero
before any outbox request or Matrix event. See [Testing](TESTING.md).
