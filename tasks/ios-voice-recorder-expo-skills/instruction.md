Build a simple voice recording app with Expo SDK 57 in `/app/voice-recorder`.

Use native components and two tabs named **Record** and **All recordings**. Record
should open first. Let me start and stop a recording, save it, play it back, and
delete it. Show elapsed recording time as `MM:SS`. Recordings should survive an
app restart. Keep everything local, with no account, server, or cloud service.
Keep the implementation small and easy to understand.

Make the controls accessible. Use the accessibility labels `Start recording`,
`Stop recording`, `Save recording`, `Play recording`, `Stop playback`, and
`Delete recording` for those actions. After stopping, saving should take one tap
with an automatic name. The empty recordings list should say `No recordings`.

Build and install the app on the local iOS simulator on this macOS machine, using
the bundle identifier `dev.expo.eval.voicerecorder`. Leave a standalone simulator
build installed that opens without Metro. Exercise recording, saving, playback,
and deletion, and save screenshots of both tabs in `/app/evidence`.

Xcode and Argent, including its skills, are available. Start a local iPhone
simulator as needed. Explain what you tested and any limits of testing microphone
input or playback on a simulator.
