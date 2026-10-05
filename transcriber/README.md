# Field Notes Transcriber

A small helper that runs on your Windows laptop and lets Field Notes transcribe recordings with speaker labels. Audio never leaves the computer.

- Speech to text: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with Whisper **large-v3-turbo** (uses an NVIDIA GPU automatically if present, otherwise the CPU)
- Who spoke when: [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) speaker diarization (pyannote segmentation 3.0 + NeMo TitaNet-Large speaker embeddings)
- Reads any audio or video: Field Notes WAV, phone voice memos, Teams/Zoom/Meet MP4, MP3, M4A

## Install, update, or set up another computer

The same installer does all three. No admin rights needed.

1. Download **[Install Field Notes Transcriber.cmd](https://martinmfranklin.github.io/meeting-recorder/transcriber/Install-Field-Notes-Transcriber.cmd)** (Field Notes on a computer also shows a *Download installer* link when the transcriber is missing or out of date).
2. Double-click it. If Windows SmartScreen warns, choose **More info → Run anyway** (Chrome may also ask you to **Keep** the file).
3. Leave the window open until it says **INSTALL FINISHED**. First time on a computer: 10 to 20 minutes (about 1.8 GB of models). Updates: a minute or two. A log is written to `Downloads\FieldNotes-install-log.txt`.

Or, in **PowerShell**:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/martinmfranklin/meeting-recorder/main/transcriber/install.ps1 | iex"
```

It installs Python 3.12 for your user if needed, sets up its own environment in `%LOCALAPPDATA%\FieldNotesTranscriber`, downloads the models (about 1.8 GB, once), starts the helper now and at every sign-in (hidden), and adds **Send to > Transcribe (Field Notes)** to the right-click menu.

Check it is running: open <http://127.0.0.1:8787> in a browser. The page lists recent jobs; **Remove** or **Clear finished and failed** tidies that list (saved transcripts are kept).

## Use

1. Open Field Notes in **Chrome or Edge** on the same computer.
2. For a phone recording or a Teams/Zoom file: **Import file** (next to Saved recordings). Select the audio or video, plus its `_notes.txt` from Field Notes if you have it; title, attendees and notes come back with it.
3. Open the recording's **Details** and click **Transcribe**. On auto it finds at least as many speakers as there are attendees listed. If more people spoke, enter how many (including you). Any number is treated as a minimum: it leans towards splitting one person in two rather than merging two people.
4. When it finishes, name each speaker (Play a sample to hear who it is). One person shown as two: give both the same name. Two people shown as one: raise **Speakers** under the transcript and click **Re-identify speakers**. That reuses the words already transcribed, redoes only who said what, and keeps the names you gave.
5. **Copy transcript**, or **Share / Save file**: the `_transcript.txt` is included, with your notes placed at the time you wrote them.

The first time, Chrome may ask whether Field Notes can access apps on this device. Choose **Allow**; that is how the page reaches the helper.

## Right-click any file (no Field Notes needed)

In File Explorer, select one or more audio or video files (Teams/Zoom/Meet MP4, voice memos, WAV...), right-click > **Send to** > **Transcribe (Field Notes)**. A small window shows progress; when done it writes, next to each file:

- `name_transcript.txt`: speakers as Speaker 1, 2... with timestamps (and, for Field Notes recordings, the title, attendees and notes)
- `name.srt`: subtitles, for video files

The transcript opens when it is finished. Existing files are never overwritten (`name_transcript (2).txt`). To name the speakers, import the file into Field Notes instead.

From a terminal: `"%LOCALAPPDATA%\FieldNotesTranscriber\venv\Scripts\python.exe" "%LOCALAPPDATA%\FieldNotesTranscriber\transcribe.py" file.mp4 [--speakers 3] [--srt]`

## OneDrive inbox (phone recordings and Teams)

The transcriber watches **`OneDrive\Field Notes Inbox`** (created automatically). Anything saved there is transcribed on this computer as soon as it has synced, then appears in Field Notes on this computer by itself, with its transcript. The originals move to `Field Notes Inbox\Imported`.

- **From the phone:** tap **Send to OneDrive** on the recording, then **Save to Files** → OneDrive → **Field Notes Inbox**. It is one WAV file with the title, attendees and notes inside it. The phone remembers the folder after the first time. (Needs the OneDrive app on the phone. An hour of audio is about 115 MB, so prefer Wi-Fi.)
- **Teams recordings:** in Field Notes, click **OneDrive inbox: on** and tick **Also pick up Teams recordings**. New files in `OneDrive\Recordings` (where Teams saves meetings you record) are picked up; recordings already there are skipped, and Teams files are never moved.
- Every inbox or Teams recording also gets a transcript in **`OneDrive\Field Notes Inbox\Transcripts`**, so you can read it anywhere OneDrive syncs, including your phone. When you name the speakers in Field Notes, that copy is updated with the names.
- Field Notes collects new items whenever it is open on this computer (every 20 seconds and when you switch back to it). Transcription itself runs even if Field Notes is closed.
- **Deleting** an inbox or Teams recording in Field Notes on this computer also deletes its OneDrive copies: the audio and notes in `Imported` and the transcript in `Transcripts` (Teams' own recording is left alone). OneDrive keeps them in its recycle bin for a while if you need one back.
- To turn the inbox off, start the helper with `--no-inbox`.

## Resource use

The background helper is small: about 40 MB of memory and no processor time while waiting. The speech model runs in a separate process that starts when there is work (about 10 to 30 seconds to load) and is stopped after 10 idle minutes, which returns its memory (roughly 1 to 1.5 GB on CPU, plus about 1.6 GB of graphics memory with an NVIDIA GPU). Change the idle time with `--idle-minutes N` on the Startup shortcut, or `--idle-minutes 0` to keep the model loaded.

## Speed

Roughly, for one hour of audio: a few minutes with an NVIDIA GPU, 20 to 40 minutes on a laptop CPU. You can close the Details panel or switch windows; the transcript appears when it is done. Jobs are queued one at a time.

## Privacy and security

- The helper listens only on `127.0.0.1` (this computer), not the network.
- It only accepts requests from the Field Notes page (and `localhost` for testing); other websites are refused.
- Uploaded audio is deleted after transcription. Finished transcripts are kept for 7 days in `%LOCALAPPDATA%\FieldNotesTranscriber\jobs` so Field Notes can collect them, and removed once collected.
- The recognised words and their timings (no audio) are kept for 14 days in `%LOCALAPPDATA%\FieldNotesTranscriber\cache` so **Re-identify speakers** does not have to transcribe again. Delete that folder at any time.

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
