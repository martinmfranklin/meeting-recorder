# Meeting Recorder

Phone-friendly web app for recording in-person meetings for later transcription.

- Records mono speech (64 kbps, about 29 MB per hour) with the phone's noise suppression off so distant voices are kept.
- Saves to the phone every 5 seconds; nothing is uploaded anywhere. Recordings stay in the browser's local storage until shared or deleted.
- Pause, time markers with notes, and an attendee list (Name, Role, Company).
- Share exports the audio (.m4a on iPhone, .m4a/.webm on Android) plus a `_notes.txt` with attendees and markers, ready for Buzz or another Whisper-based transcriber.

## Use

Open the GitHub Pages URL on your phone in Safari or Chrome, then Add to Home Screen. Keep the screen on while recording; on iPhone, locking the screen stops the microphone.
