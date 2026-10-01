# Field Notes Transcriber

A small helper that runs on your Windows laptop and lets Field Notes transcribe recordings with speaker labels. Audio never leaves the computer.

- Speech to text: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with Whisper **large-v3-turbo** (uses an NVIDIA GPU automatically if present, otherwise the CPU)
- Who spoke when: [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) speaker diarization (pyannote segmentation 3.0 + NeMo TitaNet speaker embeddings)
- Reads any audio or video: Field Notes WAV, phone voice memos, Teams/Zoom/Meet MP4, MP3, M4A

## Install (once)

Open **PowerShell** (no admin needed) and run:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/martinmfranklin/meeting-recorder/main/transcriber/install.ps1 | iex"
```

It installs Python 3.12 for your user if needed, sets up its own environment in `%LOCALAPPDATA%\FieldNotesTranscriber`, downloads the models (about 1.7 GB, once), and starts the helper now and at every sign-in, hidden.

Check it is running: open <http://127.0.0.1:8787> in a browser.

## Use

1. Open Field Notes in **Chrome or Edge** on the same computer.
2. For a phone recording or a Teams/Zoom file: **Import file** (next to Saved recordings). Select the audio or video, plus its `_notes.txt` from Field Notes if you have it; title, attendees and notes come back with it.
3. Open the recording's **Details** and click **Transcribe**. Leave Speakers on auto, or enter the number of people who spoke if you know it.
4. When it finishes, name each speaker (Play a sample to hear who it is). Giving two speakers the same name merges them.
5. **Copy transcript**, or **Share / Save file**: the `_transcript.txt` is included, with your notes placed at the time you wrote them.

The first time, Chrome may ask whether Field Notes can access apps on this device. Choose **Allow**; that is how the page reaches the helper.

## OneDrive inbox (phone recordings and Teams)

The transcriber watches **`OneDrive\Field Notes Inbox`** (created automatically). Anything saved there is transcribed on this computer as soon as it has synced, then appears in Field Notes on this computer by itself, with its transcript. The originals move to `Field Notes Inbox\Imported`.

- **From the phone:** tap **Send to OneDrive** on the recording, then **Save to Files** → OneDrive → **Field Notes Inbox**. It is one WAV file with the title, attendees and notes inside it. The phone remembers the folder after the first time. (Needs the OneDrive app on the phone. An hour of audio is about 115 MB, so prefer Wi-Fi.)
- **Teams recordings:** in Field Notes, click **OneDrive inbox: on** and tick **Also pick up Teams recordings**. New files in `OneDrive\Recordings` (where Teams saves meetings you record) are picked up; recordings already there are skipped, and Teams files are never moved.
- Field Notes collects new items whenever it is open on this computer (every 20 seconds and when you switch back to it). Transcription itself runs even if Field Notes is closed.
- To turn the inbox off, start the helper with `--no-inbox`.

## Speed

Roughly, for one hour of audio: a few minutes with an NVIDIA GPU, 20 to 40 minutes on a laptop CPU. You can close the Details panel or switch windows; the transcript appears when it is done. Jobs are queued one at a time.

## Privacy and security

- The helper listens only on `127.0.0.1` (this computer), not the network.
- It only accepts requests from the Field Notes page (and `localhost` for testing); other websites are refused.
- Uploaded audio is deleted after transcription. Finished transcripts are kept for 7 days in `%LOCALAPPDATA%\FieldNotesTranscriber\jobs` so Field Notes can collect them, and removed once collected.

## Options

`server.py --model large-v3` (more accurate, slower), `--model medium.en`, `--device cpu`, `--port 8787`. Edit the shortcut in your Startup folder to change them.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\FieldNotesTranscriber\uninstall.ps1"
```

## Troubleshooting

- **Field Notes says the transcriber is not running**: open <http://127.0.0.1:8787>. If it does not load, start *Field Notes Transcriber* from the Start menu. The log is `%LOCALAPPDATA%\FieldNotesTranscriber\transcriber.log`.
- **GPU not used**: the status page shows CPU or GPU. The log explains why the GPU could not be used (usually missing NVIDIA libraries; re-run the installer).
- **Company laptop blocks installs**: the installer needs winget or an existing Python 3.10 to 3.12, and permission to run a background program. Ask IT if either is blocked.
