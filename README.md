# Meeting Recorder

Phone-friendly web app for recording in-person meetings for later transcription.

- Records mono speech (64 kbps, about 29 MB per hour) with the phone's noise suppression off so distant voices are kept.
- Saves to the phone every 5 seconds; nothing is uploaded anywhere. Recordings stay in the browser's local storage until shared or deleted.
- Pause, typed markers (Action, Decision, Question, Follow-up) with notes, and an attendee list (Name, Role, Company).
- Dark: pure black screen that keeps the phone awake and blocks accidental taps. Hold for 1 second to return.
- Silent and discreet: no sounds or vibration. While recording the page shows only your notes and a small status dot; tap the dot to see elapsed time for 3 seconds.
- Interruption screen: if a call or another app takes the mic, a full-screen Resume button appears (no sound). Resumed audio is saved as a new part.
- Check setup: 5-second mic test with playback, plus storage, battery, screen-awake and offline status.
- Works offline after the first visit (service worker).
- Share exports the audio (.m4a on iPhone, .m4a/.webm on Android) plus a `_notes.txt` with attendees, markers in order, and Actions / Decisions / Open questions / Follow-ups grouped, ready for Buzz or another Whisper-based transcriber.

## Use

Open the GitHub Pages URL on your phone in Safari or Chrome, then Add to Home Screen. Keep the screen on while recording; on iPhone, locking the screen stops the microphone.
