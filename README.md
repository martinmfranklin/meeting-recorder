# Meeting Recorder

Phone-friendly web app for recording in-person meetings for later transcription.

- Records 16 kHz mono 16-bit WAV (about 115 MB per hour), the format Whisper uses internally. Audio is captured directly with an AudioWorklet rather than the browser's MediaRecorder, which drops audio on iPhone Safari. The phone's noise suppression is off so distant voices are kept.
- Saves to the phone every 5 seconds; nothing is uploaded anywhere. Recordings stay in the browser's local storage until deleted; Delete frees the space. Copies you shared or saved elsewhere (Files, OneDrive) are separate and are not deleted from the phone.
- Saved recordings are grouped by day (Today, Yesterday, This week, Last week, then by month; older months fold away), with search across titles, attendees, notes and transcripts once there are a few.
- Pause, bullet notes stamped with their time in the audio, and an attendee list (Name, Role, Company).
- Auto level (on by default): 90 Hz rumble filter, then a gain that adjusts itself during the meeting (tracks background noise, detects speech, moves up to 2 dB/s up and 6 dB/s down, holds during silence, capped so noise is not boosted too far), then a leveller and limiter. The last gain is remembered for the next recording.
- Continue: add more recording to a saved meeting later. All parts export as one WAV; the notes file lists where each part starts.
- Dark: pure black screen that keeps the phone awake and blocks accidental taps. Hold for 1 second to return.
- Silent and discreet: no sounds or vibration. While recording the page shows only your notes and a small status dot; tap the dot to see elapsed time for 3 seconds.
- Interruption screen: if a call or another app takes the mic, a full-screen Resume button appears (no sound). Resumed audio is saved as a new part.
- Check setup: 5-second mic test with playback, plus storage, battery, screen-awake and offline status.
- Microphone choice: pick the mic under Microphone (the browser cannot see which mic Teams uses). The choice is remembered; if it is not connected, the app warns before recording instead of quietly using another mic. Test mic shows a live level for 10 seconds. The mic used is listed in the notes file.
- Works offline after the first visit (service worker).
- Share exports the audio (.wav) plus a `_notes.txt` with attendees, recording breaks, and bullet notes with audio timestamps, ready for Buzz or another Whisper-based transcriber.

## Call mode (Windows laptop)

Choose **Call** before starting to record Teams, Zoom or Google Meet. In the share window pick **Entire screen** and switch on **Share system audio**; the call audio is mixed with your microphone into one WAV. Use Chrome or Edge, and a headset so the other side is not picked up twice. Clicking Stop sharing in the browser bar stops the recording; Resume asks to share again and continues in a new part.

## Use

Open the GitHub Pages URL on your phone in Safari or Chrome, then Add to Home Screen. Keep the screen on while recording; on iPhone, locking the screen stops the microphone.

## Transcription (Windows laptop)

`transcriber/` is a local helper that lets Field Notes transcribe recordings with speaker labels on your own computer (Whisper large-v3-turbo + sherpa-onnx speaker diarization). See [transcriber/README.md](transcriber/README.md) to install. Field Notes gains **Import file** (phone recordings, Teams/Zoom/Meet files) and **Details → Transcribe**, speaker naming, and a `_transcript.txt` in Share.
