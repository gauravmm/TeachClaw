# Audio Message Support

## Status quo

When a WhatsApp voice message or audio file arrives, the bridge extracts metadata (mime type,
size, PTT flag) but does **not** download the audio bytes. The Python channel receives
`[Voice Message]` or `[Audio]` as placeholder text, logs a warning, and forwards the stub to
the agent loop. The model sees the placeholder but has no access to the audio content.

Relevant code paths today:

- `bridge/src/whatsapp.ts` lines 408-414 — extracts `audioMessage`, emits metadata-only stub.
- `bridge/src/whatsapp.ts` lines 186-201 — image download path (pattern to mirror for audio).
- `benchclaw/channels/whatsapp/channel.py` lines 181-184 — logs "not yet supported".
- `benchclaw/channels/whatsapp/channel.py` lines 244-294 — image save path (pattern to mirror).
- `benchclaw/providers/transcription.py` — unused Groq Whisper provider (to be deleted).
- `benchclaw/media.py` — `MediaRepository`, already handles arbitrary `media_type` values.
- `benchclaw/agent/tools/media.py` — image annotation/search tools.

## Goals

1. Download voice messages and audio files from WhatsApp, save them to `workspace/media/`.
2. Pass the audio to the LLM as a native audio content block (like images today).
3. Have the LLM annotate audio on arrival, producing a rich description for search and memory.
4. Extend the media tools so the model can search, annotate, and replay audio.

## Design

### Layer 1: Bridge — download audio bytes

Mirror the existing image download path. In `whatsapp.ts`, after detecting `audioMessage`:

```
if (message.audioMessage && this.sock) {
  const buffer = await downloadMediaMessage(msg, 'buffer', {}, {
    logger, reuploadRequest: this.sock.updateMediaMessage,
  });
  if (buffer) {
    outMsg.mediaBase64 = buffer.toString('base64');
    outMsg.mediaType = message.audioMessage.mimetype || 'audio/ogg; codecs=opus';
  }
}
```

WhatsApp voice messages are OGG/Opus (`.ogg`). Regular audio shares are usually MP3 or M4A.
Both are small enough to base64-encode over the WebSocket (voice messages are typically 10-300 KB;
even a 5-minute clip at WhatsApp's bitrate is ~600 KB, well under WebSocket frame limits).

The `ptt` flag (push-to-talk) distinguishes voice notes from forwarded audio files.
Preserve this in `mediaMetadata` so downstream layers can label them differently.

**Decision: download in the bridge, not in Python.**
The bridge already has an active Baileys socket with the decryption keys needed by
`downloadMediaMessage`. Downloading in Python would require re-implementing the Baileys
media download protocol or exposing a media-fetch RPC, both more complex.

**Decision: base64 over WebSocket (same as images).**
An alternative is streaming audio to a file in the bridge and sending a file path. This would
avoid base64 overhead (~33%) but requires shared filesystem access between the Node.js bridge
and the Python process. Since the system already uses base64 for images and audio files are
comparable in size, using the same transport keeps the bridge protocol uniform. If audio files
grow large (e.g. forwarded podcasts), a future optimization can add a size threshold that falls
back to file-based transfer.

### Layer 2: Python channel — save and register

In `WhatsAppChannel._handle_bridge_inbound`, extend or generalize `_save_bridge_image` into a
`_save_bridge_media` method that handles both images and audio:

```python
def _save_bridge_media(self, event, sender_id, source_ts, media_metadata):
    if not event.mediaBase64 or not self.media_repo:
        return []

    mime_type = event.mediaType or "application/octet-stream"
    media_type = _media_type_from_mime(mime_type)  # "image", "audio", etc.
    ext = _ext_from_mime(mime_type)                 # ".jpg", ".ogg", ".mp3", etc.

    file_path = self.media_repo.register(
        event.chatId.as_address(),
        sender_id=sender_id,
        media_type=media_type,
        ext=ext,
        mime_type=mime_type,
        timestamp=source_ts,
        original_name=...,
    )
    file_path.write_bytes(base64.b64decode(event.mediaBase64))
    # ... update media_metadata[0] with path and saved_at ...
    return [self.media_repo.media_relpath(file_path)]
```

This replaces the current `_save_bridge_image` and makes media saving generic for any future
media type (video, documents) without further refactoring.

**Extension map** (new entries for audio):

| MIME type                    | Extension | media_type |
|------------------------------|-----------|------------|
| `audio/ogg; codecs=opus`     | `.ogg`    | `audio`    |
| `audio/ogg`                  | `.ogg`    | `audio`    |
| `audio/mpeg`                 | `.mp3`    | `audio`    |
| `audio/mp4`                  | `.m4a`    | `audio`    |
| `audio/aac`                  | `.aac`    | `audio`    |

### Layer 3: LLM context — native audio blocks

Today, images are injected into the LLM message as `image_url` content blocks with base64
data URIs. Audio should follow the same pattern using the provider's native audio content
block format.

For Anthropic Claude, the audio block format is:

```json
{
  "type": "input_audio",
  "source": {
    "type": "base64",
    "media_type": "audio/ogg",
    "data": "<base64>"
  }
}
```

**Changes needed:**

1. **`MediaRepository.audio_block(path)`** — new method, analogous to `image_block()`. Reads
   the file, base64-encodes it, and wraps it in the provider-appropriate audio block format.

2. **`MediaRepository.build_media_blocks(paths)`** — replace `build_image_blocks` with a
   version that dispatches on mime type: images produce `image_url` blocks, audio produces
   `input_audio` blocks. Remove `build_image_blocks` and update all call sites.

3. **`Session.render_llm_messages`** — currently uses `pending_image_paths` / `pending_image_blocks`.
   Generalize to `pending_media_paths` / `pending_media_blocks`. The `UserEvent.to_llm_message`
   method already accepts arbitrary content blocks, so no change needed there.

4. **`AgentLoop._process_llm_turn`** — update to pass `pending_media` instead of
   `pending_images`.

**Decision: send audio natively to the LLM, not as a transcript.**
The spec assumes the selected model supports audio input. Sending native audio preserves
tone, emphasis, language, and speaker identity that a transcript would lose. This is
especially important for multilingual conversations where the transcription model might
introduce errors. The model produces the annotation itself (Layer 4), so there is no
separate transcription pipeline.

**Decision: do not transcode audio.**
WhatsApp's OGG/Opus and MP3 formats are supported by Anthropic Claude and most other major
providers. Transcoding adds latency, a `ffmpeg` dependency, and potential quality loss.
If a specific provider rejects a format, handle it in the provider layer (not the channel).

**Tradeoff: context window cost.**
Audio content blocks consume tokens. A 30-second voice note is roughly 500-1000 tokens in
Anthropic's accounting. Unlike images, audio cannot be visually "glanced at" — the model
must process the full duration. Mitigation: audio blocks are only included as
`pending_media` on the turn they arrive (same as images today), so they do not persist
across turns. The annotation stored in `.media.json` serves as the durable memory.

### Layer 4: Annotation — model-authored descriptions

The LLM itself produces the annotation for audio messages, just as it already does for
images via `annotate_media`. There is no separate transcription pipeline.

**How it works:**
The model receives the native audio block and the `[Voice Message]` text label. The system
prompt already instructs the model to call `annotate_media` on received media. When it does
so for audio, it writes a caption that can include a transcript, a summary, tone/intent
notes, language identification, or whatever is germane to the conversation — all richer
than a mechanical Whisper transcript.

**Message content stays minimal:**
The channel sets `content = "[Voice Message]"` (or `"[Audio]"`), same as today. No
transcript is injected at ingestion time. The model's first LLM turn sees the audio
natively and can respond directly. The `annotate_media` call persists a searchable
description to `.media.json`.

**Decision: provider-side annotation, not a separate transcription service.**
A dedicated Whisper call produces a bare transcript — accurate but context-free. The LLM
hears the same audio and can produce a description that accounts for the conversation
context, speaker identity, and what details actually matter. This also eliminates the Groq
dependency and the ingestion-time latency of a transcription API call. The existing
`GroqTranscriptionProvider` (`providers/transcription.py`) can be deleted.

**Tradeoff: annotation is not immediate.**
The caption is empty until the model's response comes back with an `annotate_media` call.
In practice this window is seconds. This is the same behavior images have today — captions
are model-authored, not pre-populated.

**Tradeoff: annotation quality depends on the model.**
A weaker or non-audio-capable model might produce a poor or missing annotation. This is
acceptable: the spec assumes the selected model supports audio, and the annotation quality
is bounded by the same model that drives the entire conversation. If the model can't handle
audio well, that's a model selection problem, not an architecture problem.

### Layer 5: Agent tools

**Rename and generalize:**

- `read_image` → `read_media`. For images, returns an `image_url` block (unchanged). For
  audio, returns an `input_audio` block so the model can re-listen.
- `send_image` → `send_media`. For audio, sends via the outbound message bus. (Outbound
  audio sending is out of scope for this spec but the tool should accept audio paths.)
- `annotate_media` — no changes needed, already media-type agnostic.

Drop the old names entirely. Update the system prompt and any tool registration code that
references the old names.

**No new tools needed.** The existing tool set covers the required operations once generalized.

## Data flow summary

```
WhatsApp voice message
    │
    ▼
Bridge (whatsapp.ts)
    ├── downloadMediaMessage → base64
    ├── mediaType = "audio/ogg; codecs=opus"
    ├── mediaMetadata = [{media_type: "voice", ptt: true, ...}]
    └── sends JSON over WebSocket
    │
    ▼
Python channel (channel.py)
    ├── _save_bridge_media → workspace/media/{hash8}/{MMDD}/{HHMM}-{serial}.ogg
    ├── registers MediaEntry in .media.json (caption: null)
    ├── content = "[Voice Message]"
    └── publishes InboundMessage with media=[path], media_metadata=[...]
    │
    ▼
Agent loop (loop.py)
    ├── pending_media = [path]
    ├── build_media_blocks(paths) → [input_audio block]
    └── LLM call with [audio block, text block] in user message
    │
    ▼
LLM response
    ├── model "hears" the audio natively
    ├── calls annotate_media(path, description) → caption saved to .media.json
    └── responds to the user based on audio content
```

## Implementation order

1. **Bridge audio download** — extend `whatsapp.ts` to download `audioMessage` via
   `downloadMediaMessage` and attach as `mediaBase64`.
2. **Generalize `_save_bridge_image`** — rename to `_save_bridge_media`, support audio
   mime types and extensions.
3. **`MediaRepository.audio_block`** and `build_media_blocks` — add audio block
   construction, generalize the image-only block builder.
4. **Generalize session rendering** — rename `pending_image_paths` →
   `pending_media_paths`, update `_build_pending_image_blocks` to use
   `build_media_blocks`.
5. **Generalize agent tools** — rename `read_image`/`send_image` to `read_media`/`send_media`,
   drop the old names.
6. **Delete `providers/transcription.py`** — no longer needed.

## Scope boundaries

**In scope:**
- WhatsApp voice messages (PTT) and audio file shares.
- Saving audio to `workspace/media/`.
- Native audio content blocks to the LLM.
- Model-authored annotation via `annotate_media`.
- Generalizing media tools and session rendering.
- Deleting the unused `GroqTranscriptionProvider`.

**Out of scope (future work):**
- Video download and frame extraction.
- Document download and OCR/parsing.
- Outbound audio (sending voice messages from the bot).
- Speaker diarization.
- Audio from non-WhatsApp channels (Telegram voice, email attachments).
- Transcoding for providers that don't support OGG/Opus.

## Open questions

1. **Size limit?** Should there be a maximum audio file size for base64 transport? WhatsApp
   caps voice messages at ~15 minutes. A 15-minute voice note at WhatsApp's bitrate is ~2 MB,
   which is ~2.7 MB base64 — acceptable over a local WebSocket, but worth monitoring. A
   configurable `max_audio_bytes` in `WhatsAppConfig` (default: 5 MB) would be prudent.

2. **Duplicate handling.** WhatsApp sometimes delivers the same message twice (especially on
   reconnect). The bridge deduplicates by message ID, but if dedup fails, the same audio would
   be saved twice. The media registry's serial-based naming prevents file collisions but not
   wasted storage. Worth adding message-ID-based dedup in the channel layer.
