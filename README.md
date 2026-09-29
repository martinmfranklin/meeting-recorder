# Meeting Recorder

Phone-friendly web app for recording in-person meetings for later transcription.

- Records 16 kHz mono 16-bit WAV (about 115 MB per hour), the format Whisper uses internally. Audio is captured directly with an AudioWorklet rather than the browser's MediaRecorder, which drops audio on iPhone Safari. The phone's noise suppression is off so distant voices are kept.
- Saves to the phone every 5 seconds; nothing is uploaded anywhere. Recordings stay in the browser's local storage until shared or deleted.
- Pause, bullet notes stamped with their time in the audio, and an attendee list (Name, Role, Company).
- Boost quiet voices (on by default): 90 Hz rumble filter, +12 dB gain, leveller and limiter on the microphone, so distant speakers come up toward the near ones.
- Continue: add more recording to a saved meeting later. All parts export as one WAV; the notes file lists where each part starts.
- Dark: pure black screen that keeps the phone awake and blocks accidental taps. Hold for 1 second to return.
- Silent and discreet: no sounds or vibration. While recording the page shows only your notes and a small status dot; tap the dot to see elapsed time for 3 seconds.
- Interruption screen: if a call or another app takes the mic, a full-screen Resume button appears (no sound). Resumed audio is saved as a new part.
- Check setup: 5-second mic test with playback, plus storage, battery, screen-awake and offline status.
- Works offline after the first visit (service worker).
- Share exports the audio (.wav) plus a `_notes.txt` with attendees, recording breaks, and bullet notes with audio timestamps, ready for Buzz or another Whisper-based transcriber.

## Call mode (Windows laptop)

Choose **Call** before starting to record Teams, Zoom or Google Meet. In the share window pick **Entire screen** and switch on **Share system audio**; the call audio is mixed with your microphone into one WAV. Use Chrome or Edge, and a headset so the other side is not picked up twice. Clicking Stop sharing in the browser bar stops the recording; Resume asks to share again and continues in a new part.

## Use

Open the GitHub Pages URL on your phone in Safari or Chrome, then Add to Home Screen. Keep the screen on while recording; on iPhone, locking the screen stops the microphone.
